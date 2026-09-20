"""Protocol lifecycle is delegated to the official MCP SDK."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .contracts import ToolSpec


class MCPConnection:
    def __init__(
        self, *, command=None, args=None, url=None, env_allow=None, headers=None, timeout=30, auth=None
    ):
        if bool(command) == bool(url):
            raise ValueError("configure exactly one of MCP command or URL")
        self.command, self.args, self.url = command, args or [], url
        self.env_allow, self.headers, self.timeout = env_allow or [], headers or {}, timeout
        self.auth = auth
        self.queue = asyncio.Queue(maxsize=100)
        self.task = None
        self.health = {"status": "disconnected", "last_error": None, "tools": 0}
        self.dirty = True
        self.discovered = {}
        self.closed = False

    @asynccontextmanager
    async def session(self):
        async def changed(message):
            value = getattr(message, "root", message)
            if getattr(value, "method", None) == "notifications/tools/list_changed":
                self.dirty = True

        if self.command:
            env = {
                k: v
                for k, v in os.environ.items()
                if k in set(self.env_allow) | {"PATH", "SYSTEMROOT", "LANG"}
            }
            params = StdioServerParameters(command=self.command, args=self.args, env=env)
            async with (
                stdio_client(params) as (read, write),
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.timeout),
                    message_handler=changed,
                ) as session,
            ):
                await session.initialize()
                yield session
        else:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.timeout, auth=self.auth
            ) as client:
                async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.timeout),
                        message_handler=changed,
                    ) as session:
                        await session.initialize()
                        yield session

    async def import_tools(self, registry, prefix, permissions):
        """Only explicitly configured remote names are imported, with locally assigned risk."""
        discovered = await self.request("discover")
        if not hasattr(registry, "mcp_owners"):
            registry.mcp_owners = {}
        for remote_name in permissions:
            name = prefix + "." + remote_name
            if name in registry.entries and registry.mcp_owners.get(name) != prefix:
                raise ValueError("MCP contribution conflicts with existing tool: " + name)
        if not hasattr(registry, "mcp_connections"):
            registry.mcp_connections = []
        if self not in registry.mcp_connections:
            registry.mcp_connections.append(self)
        for owned,owner in list(registry.mcp_owners.items()):
            if owner == prefix and owned.removeprefix(prefix+".") not in permissions:
                registry.entries.pop(owned,None)
                registry.latest.pop(owned,None)
        for remote_name, risk in permissions.items():
            remote = discovered[remote_name]
            if set(risk) != {"effect", "idempotent"}:
                raise ValueError("MCP tools require explicit effect and idempotent declarations")
            spec = ToolSpec(
                name=prefix + "." + remote_name,
                description=remote.description or "",
                input_schema=remote.inputSchema,
                output_schema={"type": "object"},
                **risk,
            )

            def make_handler(name):
                async def call(arguments, context):
                    result = await self.request("call", name, arguments)
                    if result.isError:
                        raise RuntimeError("MCP tool returned an error")
                    return result.model_dump(mode="json", exclude_none=True)

                return call

            from .components import digest

            revision = int(digest(spec.model_dump())[:12], 16)
            registry.versions[spec.name, revision] = (spec, make_handler(remote_name))
            registry.latest[spec.name] = revision
            registry.mcp_owners[spec.name] = prefix
            if spec.name in registry.entries:
                registry.entries[spec.name] = (spec, make_handler(remote_name))
            else:
                registry.register(spec, make_handler(remote_name))

    async def discover(self, session):
        discovered, cursor = {}, None
        for _ in range(100):
            result = await session.list_tools(cursor=cursor)
            discovered.update({t.name: t for t in result.tools})
            if not result.nextCursor:
                self.discovered = discovered
                self.dirty = False
                self.health["tools"] = len(discovered)
                return discovered
            cursor = result.nextCursor
        raise ValueError("MCP discovery exceeded 100 pages")

    async def request(self, method, name=None, arguments=None):
        if self.closed:
            self.closed = False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.serve(), name="eah-mcp-connection")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((method, name, arguments, future))
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def serve(self):
        pending = None
        while not self.closed:
            try:
                pending = await self.queue.get()
                async with self.session() as session:
                    self.health.update(status="connected", last_error=None)
                    self.dirty = True
                    while not self.closed:
                        method, name, arguments, future = pending
                        if not future.cancelled():
                            task = asyncio.create_task(
                                self.discover(session)
                                if method == "discover"
                                else self.call_remote(session, name, arguments)
                            )

                            def cancel_task(f, task=task):
                                if f.cancelled():
                                    task.cancel()

                            future.add_done_callback(cancel_task)
                            try:
                                value = await task
                                if not future.done():
                                    future.set_result(value)
                            except asyncio.CancelledError:
                                if self.closed:
                                    raise
                            except Exception as exc:
                                if not future.done():
                                    future.set_exception(exc)
                                raise
                        pending = None
                        pending = await self.queue.get()
            except asyncio.CancelledError:
                if pending and not pending[3].done():
                    pending[3].cancel()
                raise
            except Exception as exc:
                self.health.update(status="disconnected", last_error=type(exc).__name__)
                if pending and not pending[3].done():
                    pending[3].set_exception(ValueError("MCP connection failed: " + type(exc).__name__))
                pending = None

    async def call_remote(self, session, name, arguments):
        from jsonschema import validate

        if self.dirty:
            await self.discover(session)
        if name not in self.discovered:
            raise ValueError("remote MCP tool is no longer available")
        validate(arguments, self.discovered[name].inputSchema)
        return await session.call_tool(name, arguments)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        while not self.queue.empty():
            pending = self.queue.get_nowait()
            if not pending[3].done():
                pending[3].cancel()
        self.health["status"] = "disconnected"


def make_mcp_server(client):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("EasyAgent")

    @server.tool()
    async def submit_workflow(workflow: dict, idempotency_key: str | None = None) -> dict:
        """Submit a validated durable workflow. Write tools pause for explicit approval."""
        return await client.request("POST", "/v1/runs", workflow, idempotency_key=idempotency_key)

    @server.tool()
    async def get_run(run_id: str) -> dict:
        """Read the status, output and pending approvals of a run."""
        return await client.request("GET", "/v1/runs/" + run_id)

    @server.tool()
    async def list_tools() -> dict:
        """List registered tools and their effects."""
        return {"tools": await client.request("GET", "/v1/tools")}

    return server
