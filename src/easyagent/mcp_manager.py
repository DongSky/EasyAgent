"""MCP connection setup with official SDK OAuth/PKCE and encrypted token storage."""

import asyncio
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import Field

from .contracts import Contract
from .http_tools import HTTPTool
from .mcp_bridge import MCPConnection


class MCPProfile(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    url: str
    permissions: dict[str, dict]
    oauth: bool = False
    redirect_uri: str = "http://127.0.0.1:8765/v1/mcp/oauth/callback"
    scope: str | None = None


class VaultTokenStorage:
    def __init__(self, vault, name):
        self.vault, self.name = vault, name

    def read(self, suffix, model):
        try:
            return model.model_validate_json(self.vault.secret(self.name + suffix))
        except ValueError:
            return None

    async def get_tokens(self):
        return self.read(".tokens", OAuthToken)

    async def set_tokens(self, tokens):
        self.vault.put_secret(self.name + ".tokens", tokens.model_dump_json())

    async def get_client_info(self):
        return self.read(".client", OAuthClientInformationFull)

    async def set_client_info(self, client_info):
        self.vault.put_secret(self.name + ".client", client_info.model_dump_json())


class ManagedMCP:
    def __init__(self, hub):
        self.hub = hub
        self.profiles = {}
        self.connections = {}
        self.pending = {}
        self.tasks = {}
        self.errors = {}

    def list(self):
        return [
            {
                "id": name,
                "url": p.url,
                "oauth": p.oauth,
                "health": self.connections[name].health,
                "authorization_url": self.pending.get(name, {}).get("url"),
                "error": self.errors.get(name),
                "profile": p.model_dump(),
                "available_tools": [{"name":t.name,"description":t.description,"input_schema":t.inputSchema} for t in self.connections[name].discovered.values()],
            }
            for name, p in self.profiles.items()
        ]

    async def connect(self, options):
        p = MCPProfile.model_validate(options)
        HTTPTool(name="mcp.validate", description="Validate MCP URL", url=p.url)
        redirect = urlsplit(p.redirect_uri)
        if p.oauth and (
            redirect.scheme != "http"
            or redirect.hostname not in ("127.0.0.1", "localhost", "::1")
            or redirect.path != "/v1/mcp/oauth/callback"
        ):
            raise ValueError("OAuth callback must use the local Hub callback URL")
        for risk in p.permissions.values():
            if (
                set(risk) != {"effect", "idempotent"}
                or risk["effect"] not in ("read", "write")
                or not isinstance(risk["idempotent"], bool)
            ):
                raise ValueError("MCP tool permissions require explicit effect and idempotent")
        persisted = {r["key"]: r["value"] for r in self.hub.store.memory_search("managed-mcp", limit=100)}
        if p.id in persisted and persisted[p.id]["url"] != p.url:
            raise ValueError("use a new connection ID for a different OAuth resource")
        if p.id in self.profiles and self.profiles[p.id].url != p.url:
            raise ValueError("use a new connection ID for a different OAuth resource")
        if old := self.connections.get(p.id):
            await old.close()
        if old_task := self.tasks.get(p.id):
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
        auth = None
        if p.oauth:

            async def redirect_handler(url):
                state = parse_qs(urlsplit(url).query).get("state", [""])[0]
                if not state:
                    raise ValueError("OAuth authorization missing state")
                self.pending[p.id] = {
                    "url": url,
                    "state": state,
                    "future": asyncio.get_running_loop().create_future(),
                }

            async def callback_handler():
                return await asyncio.wait_for(self.pending[p.id]["future"], 300)

            auth = OAuthClientProvider(
                p.url,
                OAuthClientMetadata(
                    redirect_uris=[p.redirect_uri],
                    client_name="EasyAgent",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=p.scope,
                ),
                VaultTokenStorage(self.hub.connections, "mcp." + p.id),
                redirect_handler,
                callback_handler,
            )
        connection = MCPConnection(url=p.url, auth=auth)
        self.hub.store.memory_put("managed-mcp", p.id, p.model_dump(), "operator")
        self.profiles[p.id] = p
        self.connections[p.id] = connection
        self.errors.pop(p.id, None)

        async def setup():
            try:
                await connection.import_tools(self.hub.tools, "mcp." + p.id, p.permissions)
                self.hub.store.memory_put("managed-mcp", p.id, p.model_dump(), "operator")
                self.pending.pop(p.id, None)
            except Exception as exc:
                self.errors[p.id] = type(exc).__name__

        self.tasks[p.id] = asyncio.create_task(setup(), name="eah-mcp-setup")
        return {"id": p.id, "status": "connecting"}

    def callback(self, state, code):
        for pending in self.pending.values():
            if pending["state"] == state and not pending["future"].done():
                pending["future"].set_result((code, state))
                return {"authorized": True, "message": "可以关闭此页，返回工作室。"}
        raise PermissionError("unknown or already consumed OAuth state")

    async def restore(self):
        for row in self.hub.store.memory_search("managed-mcp", limit=100):
            await self.connect(row["value"])

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for connection in self.connections.values():
            await connection.close()


def install_managed_mcp(app, hub):
    @app.get("/v1/mcp/connections")
    async def listing():
        return hub.mcp.list()

    @app.post("/v1/mcp/connections", status_code=202)
    async def connect(body: MCPProfile):
        return await hub.mcp.connect(body)

    @app.post("/v1/mcp/connections/{identifier}/reload")
    async def reload(identifier: str):
        return await hub.mcp.connect(hub.mcp.profiles[identifier])

    @app.get("/v1/mcp/oauth/callback")
    async def callback(state: str, code: str):
        return hub.mcp.callback(state, code)
