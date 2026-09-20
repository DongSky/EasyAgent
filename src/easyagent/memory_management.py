"""Provenance-preserving memory consolidation with stale-read protection."""

import json
import time
from pydantic import Field
from .contracts import Contract, ToolSpec
from .components import digest
from .store import Conflict, encode


class MemoryMerge(Contract):
    destination: str = Field(min_length=1, max_length=160)
    sources: dict[str, str] = Field(min_length=1, max_length=30)
    value: object
    expires: float | None = None


class MemoryManagement:
    def __init__(self, hub):
        self.hub = hub
        with hub.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,source TEXT NOT NULL,archived REAL NOT NULL,reason TEXT NOT NULL)"
            )

        async def merge(args, ctx):
            namespace = args.pop("namespace")
            if namespace not in ctx.job["spec"]["input"].get("memory_namespaces", []):
                raise PermissionError("memory namespace not granted")
            if self.hub.backends.binding("memory", ctx.job):
                return await self.hub.backends.call(
                    "memory",
                    "merge",
                    args | {"namespace": namespace, "operation_id": ctx.invocation_id},
                    ctx.job,
                )
            return self.merge(namespace, MemoryMerge.model_validate(args), source="agent:" + ctx.run_id)

        schema = MemoryMerge.model_json_schema()
        schema["properties"]["namespace"] = {"type": "string"}
        schema["required"].append("namespace")
        hub.tools.register(
            ToolSpec(
                name="memory.merge",
                description="合并已授权的记忆，核验原文摘要，保留来源与撤销记录",
                input_schema=schema,
                effect="write",
                idempotent=True,
            ),
            merge,
        )

    def merge(self, namespace, body, source="operator"):
        body = MemoryMerge.model_validate(body)
        with self.hub.store.transaction() as db:
            rows = []
            for key, checksum in body.sources.items():
                row = db.execute(
                    "SELECT * FROM memory WHERE namespace=? AND key=?", (namespace, key)
                ).fetchone()
                if not row or digest(json.loads(row["value"])) != checksum:
                    raise Conflict("memory changed since selected: " + key)
                rows.append(row)
            if (
                body.destination not in body.sources
                and db.execute(
                    "SELECT 1 FROM memory WHERE namespace=? AND key=?", (namespace, body.destination)
                ).fetchone()
            ):
                raise Conflict("destination already exists")
            provenance = []
            for row in rows:
                cursor = db.execute(
                    "INSERT INTO memory_archive(namespace,key,value,source,archived,reason) VALUES(?,?,?,?,?,?)",
                    (
                        namespace,
                        row["key"],
                        row["value"],
                        row["source"],
                        time.time(),
                        "merge:" + body.destination,
                    ),
                )
                provenance.append(cursor.lastrowid)
                db.execute("DELETE FROM memory WHERE namespace=? AND key=?", (namespace, row["key"]))
                db.execute("DELETE FROM memory_expiry WHERE namespace=? AND key=?", (namespace, row["key"]))
            db.execute(
                "INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?)",
                (
                    namespace,
                    body.destination,
                    encode(body.value),
                    source + "; archives:" + ",".join(map(str, provenance)),
                    time.time(),
                ),
            )
            if body.expires:
                db.execute(
                    "INSERT OR REPLACE INTO memory_expiry VALUES(?,?,?)",
                    (namespace, body.destination, body.expires),
                )
        return {"merged": True, "destination": body.destination, "archive_ids": provenance}


def install_memory_management(app, hub):
    @app.post("/v1/memory/{namespace}/merge")
    async def merge(namespace: str, body: MemoryMerge):
        if hub.backends.binding("memory"):
            return await hub.backends.call("memory", "merge", {"namespace": namespace, **body.model_dump()})
        return hub.memory_management.merge(namespace, body)

    @app.get("/v1/memory/{namespace}/history")
    async def history(namespace: str):
        if hub.backends.binding("memory"):
            return (await hub.backends.call("memory", "history", {"namespace": namespace}))["items"]
        with hub.store.connect() as db:
            return [
                dict(r) | {"value": json.loads(r["value"])}
                for r in db.execute(
                    "SELECT * FROM memory_archive WHERE namespace=? ORDER BY id DESC LIMIT 100", (namespace,)
                )
            ]
