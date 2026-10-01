"""FastAPI server for EasyAgent rewrite (SPEC §8).

Endpoints (all under ``/v1``; the unprefixed paths are kept as deprecated
aliases):
    POST /v1/missions {goal, budget?}          -> {mission_id, run_id}
    GET  /v1/runs/{id}                         -> RunState
    GET  /v1/runs/{id}/events?after_seq=N       -> SSE text/event-stream
    POST /v1/runs/{id}/steer {action, message?}-> ok
    POST /v1/runs/{id}/resume                   -> {run_id} (new run from checkpoint)
    POST /v1/sse-tokens                         -> {token} (one-time SSE token)
    GET  /v1/tools                             -> registry list
    POST /v1/tools/promote {name}              -> promote result
    /                                          -> frontend/ static files

Auth: when ``EASYAGENT_API_KEY`` is set, every API route (but not ``/healthz``
or the static frontend) requires ``Authorization: Bearer <key>`` (compared in
constant time). The SSE stream additionally accepts a one-time
``?token=`` minted via ``POST /v1/sse-tokens``, because EventSource cannot
set headers; the long-term key never travels in a URL. When the env var is
unset the API is open (local-dev convenience) and a warning is logged at
startup.
"""

from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .contracts import Budget, SteerCommand
from .store import Store

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.environ.get("EASYAGENT_DB", os.path.join(_REPO_ROOT, "data", "easyagent.db"))
_FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend")

os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
_store = Store(_DB_PATH)


class ModulePending(RuntimeError):
    """Raised when a not-yet-delivered sibling module (loop/registry) is needed."""


def _load_sibling(name: str) -> Any | None:
    """Lazy import of ``easyagent.<name>``; None when the module is missing."""
    try:
        return importlib.import_module(f"easyagent.{name}")
    except ImportError:
        return None


def _require_sibling(name: str) -> Any:
    mod = _load_sibling(name)
    if mod is None:
        raise ModulePending(f"{name} module pending")
    return mod


def _entrypoint(mod: Any, *names: str) -> Any | None:
    """Duck-typed lookup: module-level function first, then a common singleton."""
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    for singleton_attr in ("registry", "default", "instance"):
        singleton = getattr(mod, singleton_attr, None)
        if singleton is not None:
            for name in names:
                fn = getattr(singleton, name, None)
                if callable(fn):
                    return fn
    return None


class MissionRequest(BaseModel):
    goal: str
    budget: Budget | None = None


class PromoteRequest(BaseModel):
    name: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the registry hot-reload watcher (1s poll) on server startup."""
    if not os.environ.get("EASYAGENT_API_KEY", "").strip():
        log.warning(
            "EASYAGENT_API_KEY is not set: the HTTP API is unauthenticated. "
            "Set it before exposing this server to a network."
        )
    reg = None
    try:
        registry_mod = _load_sibling("registry")
        if registry_mod is not None:
            get_reg = getattr(registry_mod, "get_default_registry", None)
            if callable(get_reg):
                reg = get_reg()
                watch = getattr(reg, "watch", None)
                if callable(watch):
                    watch()
    except Exception:
        pass
    yield
    try:
        stop = getattr(reg, "stop_watch", None)
        if callable(stop):
            stop()
    except Exception:
        pass


app = FastAPI(title="EasyAgent", version="0.2.0", lifespan=lifespan)


# ------------------------------------------------------------------ auth

_bearer = HTTPBearer(auto_error=False)

# One-time SSE tokens: EventSource cannot set request headers, so the
# frontend first calls POST /v1/sse-tokens (Bearer auth) and then opens the
# SSE stream with ?token=<one-time>. Tokens are single-use, expire after
# 60s, and never carry the long-term API key in a URL.
_sse_tokens: dict[str, float] = {}
_sse_tokens_lock = threading.Lock()
SSE_TOKEN_TTL_S = 60.0


def _expected_key() -> str:
    return os.environ.get("EASYAGENT_API_KEY", "").strip()


def _key_ok(presented: str) -> bool:
    expected = _expected_key()
    if not expected:
        return True  # open mode: local dev convenience (warned at startup)
    return hmac.compare_digest(presented.strip(), expected)


async def require_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Bearer auth for the API. No ``?key=`` fallback: the long-term key must
    never travel in a URL (server logs, browser history, referers)."""
    presented = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        presented = credentials.credentials or ""
    if not _key_ok(presented):
        raise HTTPException(401, "invalid or missing API key")


async def require_key_sse(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Auth for the SSE stream: Bearer auth, or a one-time ``?token=`` minted
    by POST /v1/sse-tokens."""
    presented = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        presented = credentials.credentials or ""
    if presented:
        if not _key_ok(presented):
            raise HTTPException(401, "invalid or missing API key")
        return
    if not _key_ok(""):
        token = (request.query_params.get("token") or "").strip()
        if token:
            now = time.time()
            with _sse_tokens_lock:
                exp = _sse_tokens.pop(token, 0.0)
                # opportunistic cleanup of expired tokens
                for t in [t for t, e in _sse_tokens.items() if e <= now]:
                    del _sse_tokens[t]
            if exp > now:
                return
        raise HTTPException(401, "invalid or missing API key")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": app.version}


v1 = APIRouter(prefix="/v1")


@v1.post("/sse-tokens", dependencies=[Depends(require_key)])
@app.post("/sse-tokens", deprecated=True, dependencies=[Depends(require_key)])
def mint_sse_token() -> dict:
    """Mint a single-use, short-lived token for the SSE stream.

    EventSource cannot set ``Authorization`` headers; the client calls this
    with Bearer auth, then opens ``/v1/runs/{id}/events?token=<token>``.
    The long-term API key never appears in a URL.
    """
    token = secrets.token_urlsafe(32)
    with _sse_tokens_lock:
        _sse_tokens[token] = time.time() + SSE_TOKEN_TTL_S
    return {"token": token, "expires_in": SSE_TOKEN_TTL_S}


@v1.post("/missions", status_code=201, dependencies=[Depends(require_key)])
@app.post("/missions", status_code=201, deprecated=True,
          dependencies=[Depends(require_key)])
def create_mission(req: MissionRequest) -> dict:
    """Create a mission and a run, then start the runner in the background."""
    mission = _store.create_mission(req.goal, req.budget or Budget())
    run = _store.create_run(mission.id)
    runner_status = "started"
    try:
        loop = _require_sibling("loop")
        start = _entrypoint(loop, "start_background", "start")
        if start is None:
            raise ModulePending("loop module pending (no start entrypoint)")
        start(run.run_id, mission.id)
    except ModulePending:
        runner_status = "pending"
        _store.append_event(
            run.run_id, "status", {"status": "pending", "reason": "loop module pending"}
        )
    return {"mission_id": mission.id, "run_id": run.run_id, "runner": runner_status}


@v1.get("/runs/{run_id}", dependencies=[Depends(require_key)])
@app.get("/runs/{run_id}", deprecated=True, dependencies=[Depends(require_key)])
def get_run(run_id: str) -> dict:
    run = _store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run.model_dump()


@v1.get("/runs/{run_id}/events", dependencies=[Depends(require_key_sse)])
@app.get("/runs/{run_id}/events", deprecated=True,
         dependencies=[Depends(require_key_sse)])
async def run_events(run_id: str, after_seq: int = 0):
    if _store.get_run(run_id) is None:
        raise HTTPException(404, "run not found")

    async def gen():
        seq = after_seq
        yield f"data: {json.dumps({'run_id': run_id, 'hello': True})}\n\n"
        while True:
            for event in _store.get_events(run_id, after_seq=seq):
                seq = max(seq, event.seq)
                yield f"data: {event.model_dump_json()}\n\n"
            run = _store.get_run(run_id)
            if run is not None and run.status in ("done", "failed", "cancelled"):
                # one last sweep in case events landed with the final status
                for event in _store.get_events(run_id, after_seq=seq):
                    seq = max(seq, event.seq)
                    yield f"data: {event.model_dump_json()}\n\n"
                yield 'event: end\ndata: {"run_id": "%s"}\n\n' % run_id
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


@v1.post("/runs/{run_id}/steer", dependencies=[Depends(require_key)])
@app.post("/runs/{run_id}/steer", deprecated=True,
          dependencies=[Depends(require_key)])
def steer_run(run_id: str, cmd: SteerCommand) -> dict:
    run = _store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        loop = _require_sibling("loop")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    queue = _entrypoint(loop, "queue_command")
    if queue is None:
        raise HTTPException(503, "loop module pending (no queue_command entrypoint)")
    queue(run_id, cmd)
    _store.append_event(run_id, "steer", cmd.model_dump())
    return {"ok": True, "run_id": run_id, "action": cmd.action}


@v1.post("/runs/{run_id}/resume", dependencies=[Depends(require_key)])
@app.post("/runs/{run_id}/resume", deprecated=True,
          dependencies=[Depends(require_key)])
def resume_run(run_id: str) -> dict:
    """Start a new run seeded from this run's latest checkpoint."""
    if _store.get_run(run_id) is None:
        raise HTTPException(404, "run not found")
    try:
        loop = _require_sibling("loop")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    resume = _entrypoint(loop, "resume_from_checkpoint")
    if resume is None:
        raise HTTPException(503, "loop module pending (no resume entrypoint)")
    out = resume(run_id)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "resume failed"))
    return out


@v1.get("/tools", dependencies=[Depends(require_key)])
@app.get("/tools", deprecated=True, dependencies=[Depends(require_key)])
def list_tools() -> dict:
    try:
        registry = _require_sibling("registry")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    list_tools_fn = _entrypoint(registry, "list_tools")
    if list_tools_fn is None:
        raise HTTPException(503, "registry module pending (no list_tools entrypoint)")
    return {"tools": list_tools_fn()}


@v1.post("/tools/promote", dependencies=[Depends(require_key)])
@app.post("/tools/promote", deprecated=True, dependencies=[Depends(require_key)])
def promote_tool(req: PromoteRequest) -> dict:
    try:
        registry = _require_sibling("registry")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    promote = _entrypoint(registry, "promote")
    if promote is None:
        raise HTTPException(503, "registry module pending (no promote entrypoint)")
    return {"ok": True, "name": req.name, "result": promote(req.name)}


app.include_router(v1)


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
