"""Versioned local authoring, exposed to agents only through operator-scoped grants."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from html.parser import HTMLParser

from pydantic import ValidationError
from jsonschema.exceptions import ValidationError as SchemaError

from .contracts import AgentConfig, DevelopmentGrant, ToolSpec, Workflow
from .http_tools import HTTPTool, build_http_tool, export_definition
from .store import Conflict, encode
from .tools import WaitingChildren


DEVELOPMENT_TOOLS = [
    "development.catalog",
    "development.read_document",
    "development.save_api",
    "development.get_api",
    "development.save_workflow",
    "development.get_workflow",
    "development.run_workflow",
    "development.call_api",
]


class DocumentText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag in ("p", "div", "br", "pre", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class RuntimeDevelopment:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        self.session_keys = {}
        hub.tools.refresh = self.refresh_api
        hub.tools.catalog_exclusions = lambda: self.archived_ids("api")
        for row in self.list_versions("api"):
            self.refresh_api(row["id"], row["revision"])
        self.register_tools()

    def get(self, kind, identifier, revision=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM definition_versions WHERE kind=? AND id=? "
                + ("ORDER BY revision DESC LIMIT 1" if revision is None else "AND revision=?"),
                (kind, identifier) if revision is None else (kind, identifier, revision),
            ).fetchone()
            if not row:
                # Pre-versioning Studio/assistant graphs remain accessible and editable.
                versioned = db.execute(
                    "SELECT 1 FROM definition_versions WHERE kind=? AND id=? LIMIT 1", (kind, identifier)
                ).fetchone()
                if kind == "workflow" and revision in (None, 0) and not versioned:
                    old = db.execute(
                        "SELECT value FROM memory WHERE namespace='studio-workflows' AND key=?", (identifier,)
                    ).fetchone()
                    if old:
                        return {
                            "id": identifier,
                            "revision": 0,
                            "workflow": json.loads(old[0]),
                            "scope": None,
                        }
                raise KeyError(identifier)
            return {
                "id": identifier,
                "revision": row["revision"],
                kind if kind == "workflow" else "definition": json.loads(row["body"]),
                "scope": row["scope"],
                "created": row["created"],
            }

    def archived_ids(self, kind):
        """Archive affects discovery only; pinned definitions remain executable."""
        prefix = kind + ":"
        with self.store.connect() as db:
            return {row[0][len(prefix):] for row in db.execute(
                "SELECT key FROM memory WHERE namespace='definition-archives' AND substr(key,1,?)=?",
                (len(prefix), prefix),
            )}

    def set_archived(self, kind, identifier, archived=True, *, reason=""):
        if kind not in ("api", "node", "workflow", "component"):
            raise ValueError("only API, node, workflow and component definitions can be archived")
        self.get(kind, identifier)
        key = kind + ":" + identifier
        if archived:
            self.store.memory_put("definition-archives", key, {"reason": reason}, source="operator")
        else:
            with self.store.transaction() as db:
                db.execute("DELETE FROM memory WHERE namespace='definition-archives' AND key=?", (key,))

    def workflows(self):
        """Visible saved workflows, including legacy definitions without versions."""
        hidden = self.archived_ids("workflow")
        return [self.get("workflow", item["key"])
                for item in self.store.memory_search("studio-workflows", limit=1000)
                if item["key"] not in hidden]

    def list_versions(self, kind, identifier=None, *, include_archived=True):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,revision FROM definition_versions WHERE kind=?"
                + (" AND id=?" if identifier else "")
                + " ORDER BY id,revision",
                (kind, identifier) if identifier else (kind,),
            ).fetchall()
        hidden = set() if include_archived else self.archived_ids(kind)
        return [self.get(kind, r["id"], r["revision"]) for r in rows if r["id"] not in hidden]

    def replay(self, operation_id):
        if not operation_id:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT kind,id,revision FROM definition_versions WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return self.get(row["kind"], row["id"], row["revision"]) if row else None

    def put(self, kind, identifier, body, expected_revision, *, scope=None, context=None):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,100}", identifier):
            # UUIDs used by the existing management API can begin with a digit.
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", identifier):
                raise ValueError("invalid definition identifier")
        operation_id = context.invocation_id if context else None
        with self.store.transaction() as db:
            if context:
                self.store.assert_owner(db, context.job)
            previous = db.execute(
                "SELECT id FROM definition_versions WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if previous:
                return self.replay(operation_id)
            row = db.execute(
                "SELECT revision,scope FROM definition_versions WHERE kind=? AND id=? ORDER BY revision DESC LIMIT 1",
                (kind, identifier),
            ).fetchone()
            if not row and kind == "workflow":
                legacy = db.execute(
                    "SELECT value FROM memory WHERE namespace='studio-workflows' AND key=?", (identifier,)
                ).fetchone()
                if legacy:
                    db.execute(
                        "INSERT INTO definition_versions VALUES(?,?,0,?,NULL,NULL,?)",
                        (kind, identifier, legacy[0], time.time()),
                    )
                    row = db.execute(
                        "SELECT revision,scope FROM definition_versions WHERE kind=? AND id=?",
                        (kind, identifier),
                    ).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise Conflict(f"revision conflict: expected {expected_revision}, current {current}")
            if scope is not None and row and row["scope"] != scope:
                raise PermissionError("definition belongs to a different authoring scope")
            revision = current + 1
            db.execute(
                "INSERT INTO definition_versions VALUES(?,?,?,?,?,?,?)",
                (
                    kind,
                    identifier,
                    revision,
                    encode(body),
                    scope if scope is not None else (row["scope"] if row else None),
                    operation_id,
                    time.time(),
                ),
            )
            if kind == "workflow":
                db.execute(
                    "INSERT OR REPLACE INTO memory VALUES('studio-workflows',?,?,?,?)",
                    (identifier, encode(body), "versioned-authoring", time.time()),
                )
            if context:
                self.store.event(
                    db,
                    context.run_id,
                    "definition.saved",
                    {"kind": kind, "id": identifier, "revision": revision},
                )
        return self.get(kind, identifier, revision)

    def save_api(self, definition, expected_revision=0, *, scope=None, context=None):
        if context and (old := self.replay(context.invocation_id)):
            self.refresh_api(old["id"], old["revision"])
            return old
        definition = HTTPTool.model_validate(definition)
        public = export_definition(definition)
        # Check everything before committing, including any actual credential configuration.
        build_http_tool(definition)
        if expected_revision == 0 and definition.name in self.hub.tools.entries:
            raise Conflict("tool already exists; update it with expected_revision")
        result = self.put("api", definition.name, public, expected_revision, scope=scope, context=context)
        if definition.api_key and not definition.api_key_env:
            self.session_keys[public["api_key_env"]] = definition.api_key
        self.refresh_api(result["id"], result["revision"])
        return result

    def credential(self, reference, name=None, revision=None):
        with self.store.connect() as db:
            key = f"{name}@{revision}:{reference}" if name is not None else reference
            row = db.execute(
                "SELECT value FROM memory WHERE namespace='credential-bindings' AND key=?", (key,)
            ).fetchone()
        target = json.loads(row[0]) if row else reference
        if hasattr(self.hub, "connections"):
            try:
                return self.hub.connections.secret(target)
            except ValueError:
                pass
        return (self.session_keys.get(target) if not row else None) or os.environ.get(target, "")

    def refresh_api(self, name, revision=None):
        try:
            row = self.get("api", name, revision)
        except KeyError:
            return
        registry = self.hub.tools
        version = row["revision"]
        if (name, version) not in registry.versions:
            definition = HTTPTool.model_validate(row["definition"])
            effect = definition.effect or (
                "read" if definition.method in ("GET", "HEAD", "OPTIONS") else "write"
            )
            spec = ToolSpec(
                name=name,
                description=definition.description,
                input_schema=definition.input_schema,
                output_schema=definition.output_schema,
                effect=effect,
                idempotent=effect == "read" or definition.idempotent,
            )

            async def call(arguments, context):
                with self.store.connect() as db:
                    disabled = db.execute("SELECT 1 FROM memory WHERE namespace='disabled-model-adapters' AND key=?",
                                          (f'{name}@{version}',)).fetchone()
                if disabled:
                    from .tools import ToolPreparationError
                    raise ToolPreparationError('请求未发送：模型连接已修改或删除，请重新生成工作流以使用当前连接')
                configured = definition.model_copy(deep=True)
                if configured.api_key_env and (key := self.credential(configured.api_key_env, name, version)):
                    configured.api_key = key
                    configured.api_key_env = None
                try:
                    _, handler = build_http_tool(configured)
                except ValueError as exc:
                    from .tools import ToolPreparationError
                    raise ToolPreparationError('请求未发送：API 连接配置或凭证不可用，请检查服务设置。') from exc
                return await handler(arguments, context)

            registry.versions[name, version] = (spec, call)
        if version >= registry.latest.get(name, 0):
            registry.entries[name] = registry.versions[name, version]
            registry.latest[name] = version

    def save_workflow(self, identifier, workflow, expected_revision=0, *, scope=None, context=None):
        if context and (old := self.replay(context.invocation_id)):
            return old
        from .authoring import validate_draft

        prepared = self.hub.prepare(workflow)
        validate_draft(self.hub, prepared)
        return self.put(
            "workflow", identifier, prepared.model_dump(), expected_revision, scope=scope, context=context
        )

    def prepare_grant(self, grant):
        for service in grant.services.values():
            parameters = re.findall(r"\{([^{}]+)\}", service.url)
            HTTPTool(
                name="validate.service",
                description="Validate service grant",
                method=service.methods[0],
                input_schema={
                    "type": "object",
                    "properties": {n: {"type": "string"} for n in parameters},
                    "required": parameters,
                },
                **service.model_dump(exclude={"methods"}),
            )
        for url in grant.documents:
            HTTPTool(name="validate.document", description="Validate documentation URL", url=url)
        for name in grant.tools:
            if name.startswith("development."):
                raise PermissionError("child tools cannot include development capabilities")
            self.hub.tools.spec(name, grant.tool_revisions.get(name))
            if name not in grant.tool_revisions and (revision := self.hub.tools.revision(name)):
                grant.tool_revisions[name] = revision
        if set(grant.models) - self.hub.models.bindings.keys():
            raise ValueError("development grant contains unknown models")
        for identifier, revision in grant.workflows.items():
            self.get("workflow", identifier, revision)

    def grant(self, context):
        spec = context.job["spec"]
        if spec["kind"] != "agent" or not spec["input"].get("development"):
            raise PermissionError("this step has no runtime development grant")
        return DevelopmentGrant.model_validate(spec["input"]["development"])

    @staticmethod
    def scoped(identifier, grant):
        if not identifier.startswith(grant.namespace + "."):
            raise PermissionError("definition identifier must start with " + grant.namespace + ".")

    def scoped_api(self, name, revision, grant):
        self.scoped(name, grant)
        row = self.get("api", name, revision)
        definition = HTTPTool.model_validate(row["definition"])
        if row["scope"] != grant.namespace:
            raise PermissionError("API was not created in this development scope")
        for service in grant.services.values():
            expected = service.model_dump(exclude={"methods", "effect"})
            if (
                all(getattr(definition, k) == v for k, v in expected.items())
                and definition.method in service.methods
                and definition.effect == service.effect
                and not definition.headers
            ):
                return row
        raise PermissionError("API no longer matches the granted service/credential boundary")

    def granted_workflow(self, identifier, revision, grant):
        row = self.get("workflow", identifier, revision)
        owned = identifier.startswith(grant.namespace + ".") and row["scope"] == grant.namespace
        if not owned and grant.workflows.get(identifier) != row["revision"]:
            raise PermissionError("workflow revision has not been granted for reuse")
        return row

    def check_workflow(self, workflow, grant, *, _depth=0):
        if _depth > 8:
            raise ValueError("subworkflow nesting exceeds 8 levels")
        workflow = Workflow.model_validate(workflow).model_copy(deep=True)

        def tool(name, revision=None):
            if name in grant.tools:
                pinned = grant.tool_revisions.get(name)
                if revision is not None and revision != pinned:
                    raise PermissionError("tool revision exceeds grant")
                return pinned
            row = self.scoped_api(name, revision, grant)
            return row["revision"]

        for step in workflow.steps:
            if step.kind == "goal":
                raise PermissionError("development cannot grant goal-controller authority")
            if step.kind == "retrieve":
                raise PermissionError("knowledge retrieval has not been granted to child workflows")
            if step.kind == "tool":
                step.tool_revision = tool(step.target, step.tool_revision)
            if step.kind in ("agent", "model") and step.target not in grant.models:
                raise PermissionError("child model exceeds development grant")
            if step.kind == "agent":
                config = AgentConfig.model_validate(step.input)
                if (
                    config.development
                    or config.policy
                    or config.skills
                    or config.skill_access
                    or config.skill_namespace
                    or config.knowledge
                    or config.memory_namespaces
                ):
                    raise PermissionError("child agents cannot acquire additional capabilities")
                config.tool_revisions = {
                    n: r for n in config.tools if (r := tool(n, config.tool_revisions.get(n))) is not None
                }
                step.input = config.model_dump()
            if step.compensate:
                revision = tool(step.compensate["target"], step.compensate.get("tool_revision"))
                if revision:
                    step.compensate["tool_revision"] = revision
            if step.workflow_ref:
                row = self.granted_workflow(step.workflow_ref.id, step.workflow_ref.revision, grant)
                step.workflow_ref.revision = row["revision"]
                step.body = row["workflow"]
            if step.body:
                step.body = self.check_workflow(step.body, grant, _depth=_depth + 1).model_dump()
        return workflow

    def register_tools(self):
        async def catalog(args, ctx):
            grant = self.grant(ctx)
            return {
                "namespace": grant.namespace,
                "services": {k: v.model_dump(exclude={"api_key_env"}) for k, v in grant.services.items()},
                "documents": grant.documents,
                "tools": [
                    self.hub.tools.spec(n, grant.tool_revisions.get(n)).model_dump() for n in grant.tools
                ],
                "models": grant.models,
                "shared_workflows": [self.get("workflow", n, r) for n, r in grant.workflows.items()],
                "definitions": [
                    {"kind": kind, "id": r["id"], "revision": r["revision"]}
                    for kind in ("api", "workflow")
                    for r in self.list_versions(kind, include_archived=False)
                    if r["scope"] == grant.namespace
                ],
            }

        async def document(args, ctx):
            grant = self.grant(ctx)
            if args["url"] not in grant.documents:
                raise PermissionError("document URL has not been granted")
            _, handler = build_http_tool(
                {
                    "name": "document.read",
                    "description": "Read public API documentation",
                    "url": args["url"],
                    "response_mode": "text",
                }
            )
            result = await handler({}, ctx)
            content = result["text"]
            if "html" in result["content_type"]:
                parser = DocumentText()
                parser.feed(content)
                content = " ".join(parser.parts)
            return {
                "url": args["url"],
                "text": content[:40000],
                "truncated": len(content) > 40000,
                "sha256": hashlib.sha256(result["text"].encode()).hexdigest(),
                "untrusted": True,
            }

        async def save_api(args, ctx):
            grant = self.grant(ctx)
            if old := self.replay(ctx.invocation_id):
                self.refresh_api(old["id"], old["revision"])
                return old
            service = grant.services.get(args["service"])
            if not service:
                raise PermissionError("unknown granted service")
            body = dict(args["definition"])
            self.scoped(body["name"], grant)
            reserved = {"api_key", "api_key_env", "headers", "auth_location", "auth_header", "auth_prefix"}
            if reserved.intersection(body):
                raise PermissionError("credentials/headers are supplied by the service grant, not the model")
            if body.get("url") != service.url or body.get("method", "GET") not in service.methods:
                raise PermissionError("API endpoint or method exceeds service grant")
            if body.get("effect", service.effect) != service.effect:
                raise PermissionError("model cannot change the service effect classification")
            body.update(service.model_dump(exclude={"methods"}))
            return self.save_api(body, args["expected_revision"], scope=grant.namespace, context=ctx)

        async def get_api(args, ctx):
            return self.scoped_api(args["id"], args.get("revision"), self.grant(ctx))

        async def save_workflow(args, ctx):
            grant = self.grant(ctx)
            self.scoped(args["id"], grant)
            if old := self.replay(ctx.invocation_id):
                return old
            # Prevent taking over a legacy/user-owned saved graph.
            try:
                old = self.get("workflow", args["id"])
            except KeyError:
                old = None
            if old and old["scope"] != grant.namespace:
                raise PermissionError("workflow belongs to another scope")
            workflow = self.check_workflow(args["workflow"], grant)
            return self.save_workflow(
                args["id"], workflow, args["expected_revision"], scope=grant.namespace, context=ctx
            )

        async def get_workflow(args, ctx):
            grant = self.grant(ctx)
            row = self.granted_workflow(args["id"], args.get("revision"), grant)
            self.check_workflow(row["workflow"], grant)
            return row

        async def run_workflow(args, ctx):
            row = await get_workflow(args, ctx)
            slot = int(ctx.invocation_id[:15], 16)
            with self.store.connect() as db:
                child = db.execute(
                    "SELECT child_id FROM child_runs WHERE parent_id=? AND step_id=? AND slot=?",
                    (ctx.run_id, ctx.step_id, slot),
                ).fetchone()
                parent, depth = ctx.run_id, 0
                while ancestor := db.execute(
                    "SELECT parent_id FROM child_runs WHERE child_id=?", (parent,)
                ).fetchone():
                    depth += 1
                    parent = ancestor[0]
            if depth >= 8:
                raise ValueError("subworkflow nesting exceeds 8 levels")
            if child:
                identifier = child[0]
            else:
                workflow = Workflow.model_validate(row["workflow"])
                workflow.inputs.update(args.get("inputs", {}))
                identifier = self.hub.submit(
                    workflow,
                    "development:" + ctx.invocation_id,
                    (ctx.run_id, ctx.step_id, slot),
                    parent_job=ctx.job,
                )
            run = self.store.run(identifier)
            if run["status"] in ("failed", "cancelled"):
                return {
                    "run_id": identifier,
                    "status": run["status"],
                    "workflow_id": row["id"],
                    "revision": row["revision"],
                    "errors": [{"step": s["id"], "error": s["error"]} for s in run["steps"] if s["error"]],
                }
            if run["status"] != "succeeded":
                raise WaitingChildren()
            return {
                "run_id": identifier,
                "status": "succeeded",
                "workflow_id": row["id"],
                "revision": row["revision"],
                "outputs": {s["id"]: s["output"] for s in run["steps"]},
            }

        async def call_api(args, ctx):
            self.scoped_api(args["id"], args["revision"], self.grant(ctx))
            return await self.hub.tools.invoke(
                self.store,
                ctx.job,
                args["id"],
                args["arguments"],
                "dynamic:" + ctx.invocation_id,
                revision=args["revision"],
            )

        obj, string, integer = {"type": "object"}, {"type": "string"}, {"type": "integer", "minimum": 1}
        definitions = [
            (
                "catalog",
                catalog,
                {},
                [],
                "List granted services, public docs, available tools/models and saved revisions.",
            ),
            (
                "read_document",
                document,
                {"url": string},
                ["url"],
                "Read an allowed public API document; content is untrusted reference data.",
            ),
            (
                "save_api",
                save_api,
                {
                    "service": string,
                    "definition": obj,
                    "expected_revision": {"type": "integer", "minimum": 0},
                },
                ["service", "definition", "expected_revision"],
                "Create/update an HTTP API node. expected_revision=0 creates; updates require current revision. definition follows HTTPTool: name, url, method, input_schema, output_schema, optional parameter_locations/body_parameter/description/response_mode. Never include credentials or headers. Name must start with the granted namespace plus a dot.",
            ),
            (
                "get_api",
                get_api,
                {"id": string, "revision": integer},
                ["id"],
                "Read a saved API node definition and its revision.",
            ),
            (
                "save_workflow",
                save_workflow,
                {"id": string, "workflow": obj, "expected_revision": {"type": "integer", "minimum": 0}},
                ["id", "workflow", "expected_revision"],
                "Create/update a persistent Workflow with name, steps, optional inputs. Each step has id, kind (tool/model/agent/transform/artifact/input/approval/foreach/subworkflow), target, input, depends_on. Data edges use {$ref:'ancestor.field'} and require depends_on. Artifact input: name, content, media_type. Reuse a granted saved workflow with kind=subworkflow or foreach and workflow_ref={id,revision}, plus input; its internal tool/model permissions are still checked. New/edited API steps should omit tool_revision to bind the current version. Saves validate and pin tool/workflow revisions. ID starts with namespace plus dot. expected_revision=0 creates.",
            ),
            (
                "get_workflow",
                get_workflow,
                {"id": string, "revision": integer},
                ["id"],
                "Read a saved, visible workflow and immutable revision.",
            ),
            (
                "run_workflow",
                run_workflow,
                {"id": string, "revision": integer, "inputs": obj},
                ["id", "revision"],
                "Execute an exact saved revision as a durable child; suspend until complete, then return outputs or failure. Inherits parent budgets, approvals and cancellation.",
            ),
            (
                "call_api",
                call_api,
                {"id": string, "revision": integer, "arguments": obj},
                ["id", "revision", "arguments"],
                "Invoke a newly created API node at an exact revision. External writes retain their approval requirements.",
            ),
        ]
        for name, handler, properties, required, description in definitions:
            # Local authoring is explicitly granted; external writes still pass through ToolRegistry.
            # Validation errors are observations so the agent can repair and retry its proposal.
            async def guarded(args, ctx, handler=handler):
                try:
                    return await handler(args, ctx)
                except (ValueError, KeyError, PermissionError, SchemaError) as exc:
                    message = (
                        "invalid authoring contract: "
                        + "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors())[
                            :800
                        ]
                        if isinstance(exc, ValidationError)
                        else exc.message[:1000]
                        if isinstance(exc, SchemaError)
                        else str(exc)[:1000]
                    )
                    return {"error": type(exc).__name__, "message": message}

            self.hub.tools.register(
                ToolSpec(
                    name="development." + name,
                    description=description,
                    input_schema={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                    effect="local"
                    if name in ("save_api", "save_workflow", "run_workflow", "call_api")
                    else "read",
                ),
                guarded,
            )
