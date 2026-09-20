"""Agent-written pure code: immutable candidates, integration scenarios, gated publication."""

import json
import time
import uuid
from pydantic import Field
from jsonschema import validate
from .contracts import Contract, ToolSpec
from .extension_contracts import ExtensionManifest, ExtensionPackage
from .extensions import build_package, validate_package
from .store import Conflict, encode


class CodeScenario(Contract):
    tool: str
    input: dict = Field(default_factory=dict)
    expected: object


class CodeCandidate(Contract):
    manifest: ExtensionManifest
    files: dict[str, str] = Field(
        description="Relative source filename to text. JavaScript defines global handle(request), returning {result: value}; lifecycle.* returns {result:{}}. A tool's handler matches request.method and arguments are request.params."
    )
    scenarios: list[CodeScenario] = Field(min_length=1, max_length=30)


class CodeDevelopment:
    def __init__(self, hub):
        self.hub = hub
        with hub.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS code_candidates(id TEXT PRIMARY KEY,package TEXT NOT NULL,scenarios TEXT NOT NULL,status TEXT NOT NULL,report TEXT,created REAL NOT NULL)"
            )
        self.register()

    def list(self):
        with self.hub.store.connect() as db:
            return [
                dict(r)
                | {
                    "package": json.loads(r["package"]),
                    "scenarios": json.loads(r["scenarios"]),
                    "report": json.loads(r["report"]) if r["report"] else None,
                }
                for r in db.execute("SELECT * FROM code_candidates ORDER BY created DESC LIMIT 100")
            ]

    def propose(self, body):
        body = CodeCandidate.model_validate(body)
        p = build_package(body.manifest, body.files)
        validate_package(p, {})
        # Candidate execution cannot activate hooks/providers or request host effects.
        if (
            p.manifest.runtime not in ("javascript", "wasm")
            or p.manifest.permissions
            or p.manifest.required_services
        ):
            raise PermissionError("generated code candidates use pure JS/WASM without host permissions")
        if p.manifest.hooks or p.manifest.providers or p.manifest.commands or p.manifest.services or p.manifest.backends:
            raise PermissionError("generated code candidates contribute pure tools only")
        if any(a.spec.effect != "read" for a in p.manifest.tools):
            raise PermissionError("generated pure tools must be read-only")
        if set(s.tool for s in body.scenarios) != set(a.spec.name for a in p.manifest.tools):
            raise ValueError("integration scenarios must cover every contributed tool")
        for old in self.list():
            if old["package"]["digest"] == p.digest and old["scenarios"] == [
                s.model_dump() for s in body.scenarios
            ]:
                return {"id": old["id"], "digest": p.digest, "status": old["status"]}
        identifier = uuid.uuid4().hex
        with self.hub.store.connect() as db:
            db.execute(
                "INSERT INTO code_candidates VALUES(?,?,?,'candidate',NULL,?)",
                (
                    identifier,
                    p.model_dump_json(),
                    encode([s.model_dump() for s in body.scenarios]),
                    time.time(),
                ),
            )
        return {"id": identifier, "digest": p.digest, "status": "candidate"}

    async def test(self, identifier):
        row = next((r for r in self.list() if r["id"] == identifier), None)
        if row is None:
            raise KeyError(identifier)
        p = ExtensionPackage.model_validate(row["package"])
        results = []
        for scenario in row["scenarios"]:
            action = next(a for a in p.manifest.tools if a.spec.name == scenario["tool"])
            try:
                validate(scenario["input"], action.spec.input_schema)
                output = await self.hub.extensions.call(
                    p,
                    action.handler,
                    scenario["input"],
                    initial={"state": p.manifest.initial_state, "settings": p.manifest.settings},
                )
                validate(output, action.spec.output_schema)
                results.append(
                    {"tool": scenario["tool"], "passed": output == scenario["expected"], "output": output}
                )
            except Exception as exc:
                results.append({"tool": scenario["tool"], "passed": False,
                                "error": type(exc).__name__ + ': ' + str(exc)[:1000]})
        passed = all(r["passed"] for r in results)
        with self.hub.store.connect() as db:
            db.execute(
                "UPDATE code_candidates SET status=?,report=? WHERE id=?",
                ("tested" if passed else "failed", encode(results), identifier),
            )
        return {"passed": passed, "results": results, "digest": p.digest}

    async def publish(self, identifier):
        row = next((r for r in self.list() if r["id"] == identifier), None)
        if row is None:
            raise KeyError(identifier)
        if row["status"] not in ("tested", "published"):
            raise Conflict("pass integration scenarios before publishing")
        result = await self.hub.extensions.install({"package": row["package"]})
        with self.hub.store.connect() as db:
            db.execute("UPDATE code_candidates SET status='published' WHERE id=?", (identifier,))
        return result

    def register(self):
        def grant(args, ctx, propose=False):
            raw = ctx.job["spec"]["input"].get("code_development") or {}
            if not raw.get("namespace"):
                raise PermissionError("code development requires a namespace grant")
            if propose:
                name = args["manifest"].get("id", "")
                if not name.startswith(raw["namespace"] + "_"):
                    raise PermissionError("code package ID exceeds granted namespace")
            else:
                row = next((r for r in self.list() if r["id"] == args["id"]), None)
                if not row or not row["package"]["manifest"]["id"].startswith(raw["namespace"] + "_"):
                    raise PermissionError("candidate outside granted namespace")

        async def create(args, ctx):
            grant(args, ctx, True)
            return self.propose(args)

        async def test(args, ctx):
            grant(args, ctx)
            return await self.test(args["id"])

        async def publish(args, ctx):
            grant(args, ctx)
            return await self.publish(args["id"])

        for name, fn, schema, effect in [
            ("create", create, CodeCandidate.model_json_schema(), "local"),
            (
                "test",
                test,
                {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "local",
            ),
            (
                "publish",
                publish,
                {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "write",
            ),
        ]:
            self.hub.tools.register(
                ToolSpec(
                    name="code." + name,
                    description="生成纯计算代码包、集成试跑并发布可复用工具。发布需要确认。",
                    input_schema=schema,
                    effect=effect,
                    idempotent=True,
                ),
                fn,
            )


def install_code_development(app, hub):
    @app.get("/v1/code/candidates")
    async def listing():
        return hub.code.list()

    @app.post("/v1/code/candidates", status_code=201)
    async def propose(body: CodeCandidate):
        return hub.code.propose(body)

    @app.post("/v1/code/candidates/{identifier}/test")
    async def test(identifier: str):
        return await hub.code.test(identifier)

    @app.post("/v1/code/candidates/{identifier}/publish")
    async def publish(identifier: str):
        return await hub.code.publish(identifier)
