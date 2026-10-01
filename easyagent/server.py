"""FastAPI server for EasyAgent rewrite (SPEC §8).

Endpoints:
    POST /missions {goal, budget?}          -> {mission_id, run_id}
    GET  /runs/{id}                         -> RunState
    GET  /runs/{id}/events?after_seq=N       -> SSE text/event-stream
    POST /runs/{id}/steer {action, message?}-> ok
    GET  /tools                             -> registry list
    POST /tools/promote {name}              -> promote result
    /                                       -> frontend/ static files

Calls into the ``loop`` and ``registry`` modules are lazy (duck-typed):
if the sibling worker has not delivered them yet, those endpoints answer
``503 "module pending"`` instead of crashing the import.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .contracts import Budget, SteerCommand
from .store import Store

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


app = FastAPI(title="EasyAgent", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/missions", status_code=201)
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


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = _store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run.model_dump()


@app.get("/runs/{run_id}/events")
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


@app.post("/runs/{run_id}/steer")
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


@app.get("/tools")
def list_tools() -> dict:
    try:
        registry = _require_sibling("registry")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    list_tools_fn = _entrypoint(registry, "list_tools")
    if list_tools_fn is None:
        raise HTTPException(503, "registry module pending (no list_tools entrypoint)")
    return {"tools": list_tools_fn()}


@app.post("/tools/promote")
def promote_tool(req: PromoteRequest) -> dict:
    try:
        registry = _require_sibling("registry")
    except ModulePending as exc:
        raise HTTPException(503, str(exc))
    promote = _entrypoint(registry, "promote")
    if promote is None:
        raise HTTPException(503, "registry module pending (no promote entrypoint)")
    return {"ok": True, "name": req.name, "result": promote(req.name)}


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
