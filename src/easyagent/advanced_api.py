from __future__ import annotations

from urllib.parse import quote

from typing import Any
import json

from fastapi import Header, Request, Response
from pydantic import Field

from .contracts import Contract
from .scheduling import Trigger
from .store import Conflict, encode
from jsonschema import FormatChecker, validate


class DocumentInput(Contract):
    namespace: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=1000000)
    embedding_model: str | None = None
    chunk_size: int = Field(default=800, ge=100, le=4000)


class SearchInput(Contract):
    namespace: str
    query: str
    limit: int = Field(default=5, ge=1, le=50)
    mode: str = "lexical"


class ArtifactInput(Contract):
    name: str
    content: str
    media_type: str = "text/plain"
    run_id: str | None = None


class EnableInput(Contract):
    enabled: bool


class ImproveInput(Contract):
    feedback: str = Field(min_length=1, max_length=16000)
    model: str | None = None


def install_advanced(app, hub):
    @app.post("/v1/inputs/{identifier}")
    async def respond(identifier: str, body: dict[str, Any]):
        with hub.store.transaction() as db:
            row = db.execute("SELECT * FROM input_requests WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise KeyError(identifier)
            step = db.execute("SELECT status FROM steps WHERE run_id=? AND id=?", (row["run_id"], row["step_id"])).fetchone()
            if row["status"] != "waiting" or step[0] != "waiting_input":
                raise Conflict("input request is not waiting for a response")
            validate(body, json.loads(row["schema"]), format_checker=FormatChecker())
            db.execute("UPDATE input_requests SET status='answered',output=? WHERE id=?", (encode(body), identifier))
            db.execute("UPDATE steps SET status='queued',ready_at=0 WHERE run_id=? AND id=?", (row["run_id"], row["step_id"]))
            hub.store.event(db, row["run_id"], "input.answered", {"id": identifier})
            hub.store.reconcile(db, row["run_id"])
        return {"accepted": True}

    @app.post("/v1/knowledge/documents", status_code=201)
    async def ingest(body: DocumentInput):
        return await hub.knowledge.ingest(**body.model_dump())

    @app.get("/v1/knowledge/documents")
    async def documents(namespace: str | None = None):
        return hub.knowledge.list(namespace)

    @app.delete("/v1/knowledge/documents/{identifier}")
    async def delete_document(identifier: str):
        hub.knowledge.delete(identifier)
        return {"deleted": True}

    @app.post("/v1/knowledge/search")
    async def search(body: SearchInput):
        return {"citations": await hub.knowledge.search(**body.model_dump())}

    @app.post("/v1/artifacts", status_code=201)
    async def artifact(body: ArtifactInput):
        return hub.artifacts.put(**body.model_dump())

    @app.post("/v1/artifacts/upload", status_code=201)
    async def upload_artifact(request: Request, name: str, run_id: str | None = None):
        """Authenticated raw uploads have a separate actual-byte 50 MB limit."""
        content = await request.body()
        if not content:
            raise ValueError("upload must contain file bytes")
        media_type = request.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        if run_id:
            hub.store.run(run_id)
        return hub.artifacts.put(name, content, media_type, run_id)

    @app.get("/v1/artifacts")
    async def artifacts(run_id: str | None = None):
        return hub.artifacts.list(run_id)

    @app.get("/v1/artifacts/{identifier}/content")
    async def content(identifier: str):
        info, data = hub.artifacts.get(identifier)
        # Attachments are never rendered as active HTML on the Hub origin.
        name = info["name"].replace("\\", "/").rsplit("/", 1)[-1].strip(". ") or "artifact"
        return Response(data, media_type=info["media_type"], headers={"Content-Disposition": 'attachment; filename="artifact"; filename*=UTF-8\'\'' + quote(name, safe=""),
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    @app.get("/v1/runs/{run_id}/trace")
    async def trace(run_id: str):
        run = hub.store.run(run_id)
        with hub.store.connect() as db:
            calls = [dict(r) for r in db.execute("SELECT * FROM model_calls WHERE run_id=? ORDER BY created", (run_id,))]
            invocations = [dict(r) for r in db.execute("SELECT id,tool,status,error FROM invocations WHERE run_id=?", (run_id,))]
        return {"id": run_id, "status": run["status"], "usage": run["usage"], "models": calls, "tools": invocations,
                "steps": [{k: s[k] for k in ("id", "status", "attempts", "error")} for s in run["steps"]],
                "artifacts": hub.artifacts.list(run_id), "children": run["children"]}

    @app.get("/v1/metrics")
    async def metrics():
        with hub.store.connect() as db:
            statuses = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM runs GROUP BY status")}
            calls = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM model_calls GROUP BY status")}
        return {"runs": statuses, "model_calls": calls, "workers": len(hub.workers)}

    @app.post("/v1/triggers", status_code=201)
    async def create_trigger(body: Trigger):
        return hub.scheduler.create(body)

    @app.get("/v1/triggers")
    async def triggers():
        return hub.scheduler.list()

    @app.patch("/v1/triggers/{identifier}")
    async def enable(identifier: str, body: EnableInput):
        hub.scheduler.enable(identifier, body.enabled)
        return {"enabled": body.enabled}

    @app.post("/v1/triggers/{identifier}/fire")
    async def fire(identifier: str, body: dict[str, Any], idempotency_key: str = Header()):
        return {"id": hub.scheduler.fire(identifier, body, idempotency_key)}

    @app.get("/v1/policies")
    async def policies():
        with hub.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM policies ORDER BY created DESC LIMIT 1000")]
        return [hub.evolution.get(identifier) for identifier in ids]

    @app.post("/v1/policy-families/{name}/improve")
    async def improve(name: str, body: ImproveInput):
        return await hub.evolution.improve(name, body.feedback, body.model)

    @app.delete("/v1/memory/{namespace}/{key}")
    async def forget(namespace: str, key: str):
        if hub.backends.binding("memory"):
            return await hub.backends.call("memory", "remove", {"namespace": namespace, "key": key})
        with hub.store.transaction() as db:
            db.execute("DELETE FROM memory WHERE namespace=? AND key=?", (namespace, key))
            db.execute("DELETE FROM memory_expiry WHERE namespace=? AND key=?", (namespace, key))
        return {"deleted": True}
