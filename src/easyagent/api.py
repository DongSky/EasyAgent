from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import ValidationError as SchemaError
from pydantic import Field, ValidationError as ContractError

from .contracts import Contract, EvaluationSuite, Policy, Workflow
from .store import Conflict
from .run_retry import RetryRequest, retry_run
from .automatic_execution import ContinueAutomatically, continue_automatically


class Approval(Contract):
    approved: bool


class Reconciliation(Contract):
    output: dict
    receipt: str = Field(min_length=1, max_length=4000)


class MemoryWrite(Contract):
    value: object
    source: str = "user"
    expires: float | None = None


class RequestSizeLimit:
    """Bound actual bytes, including chunked bodies with no Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            limit = 50_000_000 if scope.get("path") == "/v1/artifacts/upload" else 2_000_000
            if size > limit:
                return await JSONResponse({"detail": "request exceeds upload limit"}, status_code=413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(hub, *, token="", manage_workers=True):
    @asynccontextmanager
    async def lifespan(app):
        if manage_workers:
            await hub.start()
        try:
            yield
        finally:
            if manage_workers:
                await hub.stop()

    app = FastAPI(title="EasyAgent", version="0.1.0", lifespan=lifespan)
    app.state.hub = hub

    @app.middleware("http")
    async def boundary(request, call_next):
        if (
            token
            and not request.url.path.startswith(("/assets/", "/life-assets/", "/v1/gateway/incoming/"))
            and request.url.path
            not in ("/", "/life", "/docs", "/openapi.json", "/docs/oauth2-redirect", "/v1/mcp/oauth/callback")
        ):
            if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
                return JSONResponse({"detail": "invalid bearer token"}, status_code=401)
        host = request.url.hostname
        if not token and host not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return JSONResponse({"detail": "non-local hosts require authentication"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "cross-origin requests are disabled"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "invalid content-length"}, status_code=400)
        limit = 50_000_000 if request.url.path == "/v1/artifacts/upload" else 2_000_000
        if content_length > limit:
            return JSONResponse({"detail": "request exceeds upload limit"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "resource not found"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    async def invalid(request, exc):
        if isinstance(exc, (RequestValidationError, ContractError)):
            # Validation errors otherwise echo whole request bodies, including API keys.
            detail = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        else:
            detail = str(exc)[:1000]
        return JSONResponse({"detail": detail}, status_code=422)

    app.add_exception_handler(RequestValidationError, invalid)

    app.add_exception_handler(ValueError, invalid)
    app.add_exception_handler(SchemaError, invalid)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0", "workers": len(hub.workers)}

    @app.post("/v1/runs", status_code=201)
    async def submit(workflow: Workflow, idempotency_key: str | None = Header(default=None, max_length=200)):
        run_id = hub.submit(workflow, idempotency_key)
        return {"id": run_id, "status": hub.store.run(run_id)["status"]}

    @app.get("/v1/runs")
    async def runs(
        limit: int = Query(default=100, ge=1, le=1000),
        query: str = Query(default="", max_length=160),
        status: list[str] = Query(default=[], max_length=20),
    ):
        return hub.store.runs(limit, query=query, statuses=status)

    @app.get("/v1/runs/{run_id}")
    async def run(run_id: str, progress: bool = False):
        return hub.store.run(run_id, progress=progress)

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        hub.store.cancel(run_id)
        return hub.store.run(run_id)

    @app.post("/v1/runs/{run_id}/retry")
    async def retry(run_id: str, body: RetryRequest):
        return await retry_run(hub, run_id, body)

    @app.post('/v1/runs/{run_id}/continue-automatically')
    async def automatic_continue(run_id: str, body: ContinueAutomatically):
        return await continue_automatically(hub, run_id, body)

    @app.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(default=0, ge=0)):
        hub.store.run(run_id)
        return hub.store.events(run_id, after)

    @app.get("/v1/runs/{run_id}/stream")
    async def stream(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: int | None = Header(default=None),
    ):
        hub.store.run(run_id)

        async def generate():
            cursor = max(after, last_event_id or 0)
            while not await request.is_disconnected():
                rows = hub.store.events(run_id, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                if hub.store.run(run_id)["status"] in ("succeeded", "failed", "cancelled") and not rows:
                    break
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/v1/tools")
    async def tools():
        return hub.tools.catalog()

    @app.get("/v1/models")
    async def models():
        return hub.models.catalog()

    @app.get("/v1/skills")
    async def skills():
        return hub.skills.catalog()

    @app.post("/v1/approvals/{invocation_id}")
    async def approve(invocation_id: str, body: Approval):
        hub.tools.approve(hub.store, invocation_id, body.approved)
        return {"approved": body.approved}

    @app.post("/v1/reconciliations/{invocation_id}")
    async def reconcile(invocation_id: str, body: Reconciliation):
        hub.tools.reconcile(hub.store, invocation_id, body.output, body.receipt)
        return {"reconciled": True}

    @app.put("/v1/memory/{namespace}/{key}")
    async def memory_put(namespace: str, key: str, body: MemoryWrite):
        if hub.backends.binding("memory"):
            return await hub.backends.call(
                "memory", "put", {"namespace": namespace, "key": key, **body.model_dump()}
            )
        hub.store.memory_put(namespace, key, body.value, body.source, body.expires)
        return {"saved": True}

    @app.get("/v1/memory/{namespace}")
    async def memory_search(
        namespace: str,
        query: str = "",
        limit: int = Query(default=20, ge=1, le=100),
        include_digest: bool = False,
    ):
        rows = (
            (
                await hub.backends.call(
                    "memory", "search", {"namespace": namespace, "query": query, "limit": limit}
                )
            )["items"]
            if hub.backends.binding("memory")
            else hub.store.memory_search(namespace, query, limit)
        )
        if include_digest:
            from .components import digest

            return [row | {"digest": digest(row["value"])} for row in rows]
        return rows

    @app.post("/v1/policies", status_code=201)
    async def propose(policy: Policy):
        return hub.evolution.propose(policy)

    @app.get("/v1/policies/{identifier}")
    async def policy(identifier: str):
        return hub.evolution.get(identifier)

    @app.post("/v1/policies/{identifier}/evaluate")
    async def evaluate(identifier: str, suite: EvaluationSuite):
        return await hub.evolution.evaluate(identifier, suite)

    @app.post("/v1/policies/{identifier}/activate")
    async def activate(identifier: str):
        return hub.evolution.activate(identifier)

    @app.post("/v1/policies/{identifier}/rollback")
    async def rollback(identifier: str):
        return hub.evolution.activate(identifier, rollback=True)

    from .advanced_api import install_advanced

    install_advanced(app, hub)
    from .workflow_calls import install_workflow_calls

    install_workflow_calls(app, hub)
    from .studio import install_studio

    install_studio(app, hub)
    from .extension_api import install_extensions

    install_extensions(app, hub)
    from .sessions import install_conversations

    install_conversations(app, hub)
    from .learning import install_learning

    install_learning(app, hub)
    from .connections import install_connections

    install_connections(app, hub)
    from .mcp_manager import install_managed_mcp

    install_managed_mcp(app, hub)
    app.add_middleware(RequestSizeLimit)
    from .code_development import install_code_development

    install_code_development(app, hub)
    from .operations import install_operations

    install_operations(app, hub)
    from .maintenance import install_maintenance

    install_maintenance(app, hub)
    from .gateway import install_gateway

    install_gateway(app, hub)
    from .memory_management import install_memory_management

    install_memory_management(app, hub)
    from .skill_packages import install_skill_api

    install_skill_api(app, hub)
    from .goals import install_goals

    install_goals(app, hub)
    from .backends import install_backends

    install_backends(app, hub)
    from .execution_backends import install_execution_api

    install_execution_api(app, hub)
    from .package_sources import install_package_sources

    install_package_sources(app, hub)
    from .voice import install_voice

    install_voice(app, hub)
    from .autonomy import install_autonomy

    install_autonomy(app, hub)
    return app
