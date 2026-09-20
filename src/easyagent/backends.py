"""Dedicated replaceable backend contracts, bound to immutable extension revisions."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field
from jsonschema import validate

from .contracts import Contract, ToolSpec
from .store import encode

BackendKind = Literal["memory", "context", "terminal", "browser", "approval", "channel", "media"]
OPERATIONS = {
    "memory": ["search", "put", "remove", "merge", "history"],
    "context": ["compact"],
    "terminal": ["execute"],
    "browser": ["navigate", "snapshot", "click", "fill", "close"],
    "approval": ["present"],
    "channel": ["send"],
    "media": ["generate", "transcribe", "speak"],
}


class BackendContribution(Contract):
    kind: BackendKind
    handler: str
    operations: list[str] = Field(min_length=1)


class BackendBinding(Contract):
    extension: str
    revision: int = Field(ge=1)


class BackendRegistry:
    def __init__(self, hub):
        self.hub = hub
        self.local = {}
        self.register_tools()

    def snapshot(self):
        return {r["key"]: r["value"] for r in self.hub.store.memory_search("backend-bindings", limit=50)}

    def binding(self, kind, job=None):
        if job:
            return self.hub.store.run(job["run_id"])["spec"].get("metadata", {}).get("backends", {}).get(kind)
        return self.snapshot().get(kind)

    def select(self, kind, body):
        if kind not in OPERATIONS:
            raise ValueError("unknown backend kind")
        binding = BackendBinding.model_validate(body)
        package = self.hub.extensions.packages[(binding.extension, binding.revision)]
        if self.hub.extensions.active.get(binding.extension) != binding.revision:
            raise ValueError("activate the extension revision before selecting its backend")
        contribution = next((b for b in package.manifest.backends if b.kind == kind), None)
        if not contribution:
            raise ValueError("extension does not provide this backend")
        self.hub.store.memory_put("backend-bindings", kind, binding.model_dump(), "operator")
        return binding.model_dump()

    def reset(self, kind):
        with self.hub.store.connect() as db:
            db.execute("DELETE FROM memory WHERE namespace='backend-bindings' AND key=?", (kind,))
        return {"kind": kind, "builtin": True}

    async def call(self, kind, operation, payload, job=None):
        if kind not in OPERATIONS or operation not in OPERATIONS[kind]:
            raise ValueError("unsupported backend operation")
        binding = self.binding(kind, job)
        if binding:
            p = self.hub.extensions.packages[(binding["extension"], binding["revision"])]
            b = next(x for x in p.manifest.backends if x.kind == kind)
            if operation not in b.operations:
                raise ValueError("backend does not implement operation")
            from .tools import InvocationContext

            context = (
                InvocationContext(
                    "backend:"
                    + hashlib.sha256(
                        encode([job["run_id"], job["id"], kind, operation, payload]).encode()
                    ).hexdigest(),
                    job["run_id"],
                    job["id"],
                    job,
                    self.hub.store,
                )
                if job
                else None
            )
            result = await self.hub.extensions.call(
                p,
                b.handler,
                {"schema_version": 1, "operation": operation, "payload": payload},
                context=context,
            )
        elif kind in self.local:
            result = await self.local[kind](operation, payload, job)
        else:
            raise ValueError("connect a " + kind + " backend first")
        if not isinstance(result, dict) or len(encode(result).encode()) > 1_000_000:
            raise ValueError("backend result must be an object within 1 MB")
        if kind == "memory" and operation == "search":
            validate(
                result,
                {
                    "type": "object",
                    "properties": {"items": {"type": "array", "items": {"type": "object"}}},
                    "required": ["items"],
                },
            )
        if kind == "context":
            validate(
                result,
                {
                    "type": "object",
                    "properties": {"summary": {"type": "string", "maxLength": 32000}},
                    "required": ["summary"],
                },
            )
        if kind == "terminal":
            validate(
                result,
                {
                    "type": "object",
                    "required": ["exit_code", "stdout", "stderr"],
                    "properties": {
                        "exit_code": {"type": "integer"},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                    },
                },
            )
        if kind in ("approval", "channel"):
            validate(
                result,
                {"type": "object", "properties": {"receipt": {"type": "string"}}, "required": ["receipt"]},
            )
        return result

    def register_tools(self):
        fields = {"operation": {"type": "string"}, "payload": {"type": "object"}}
        for kind in ("terminal", "browser", "channel", "media"):

            async def handler(args, ctx, kind=kind):
                return await self.call(
                    kind, args["operation"], args["payload"] | {"operation_id": ctx.invocation_id}, ctx.job
                )

            self.hub.tools.register(
                ToolSpec(
                    name="backend." + kind,
                    description="Use the configured "
                    + kind
                    + " service. Operations: "
                    + ", ".join(OPERATIONS[kind]),
                    input_schema={
                        "type": "object",
                        "properties": fields | {"operation": {"enum": OPERATIONS[kind]}},
                        "required": ["operation", "payload"],
                        "additionalProperties": False,
                    },
                    effect="write",
                    idempotent=False,
                ),
                handler,
            )


def install_backends(app, hub):
    @app.get("/v1/backends")
    async def list_backends():
        providers = []
        for (name, revision), p in hub.extensions.packages.items():
            for b in p.manifest.backends:
                providers.append(
                    {
                        "extension": name,
                        "revision": revision,
                        **b.model_dump(),
                        "active": hub.extensions.active.get(name) == revision,
                    }
                )
        return {
            "contracts": OPERATIONS,
            "bindings": hub.backends.snapshot(),
            "providers": providers,
            "local": list(hub.backends.local),
        }

    @app.post("/v1/backends/{kind}")
    async def select(kind: str, body: BackendBinding):
        return hub.backends.select(kind, body)

    @app.delete("/v1/backends/{kind}")
    async def reset(kind: str):
        return hub.backends.reset(kind)
