"""Portable frozen workflows, including Agent/model steps and exact HTTP dependencies.

Import binds local models/data and creates an independent workflow. It never executes it,
installs executable plugins, copies credentials or activates an evolution policy.
"""

from __future__ import annotations

import copy
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field

from .authoring import check_literals
from .component_packages import PackageDefinition
from .components import digest
from .contracts import AgentConfig, Contract, ToolSpec, Workflow
from .http_tools import HTTPTool
from .store import Conflict, encode


class WorkflowPackage(Contract):
    format: str = Field(
        default="easyagent.workflow-package.v1", pattern=r"^easyagent\.workflow-package\.v1$"
    )
    source: dict
    workflow: dict
    apis: list[PackageDefinition] = Field(default_factory=list, max_length=300)
    granted_workflows: list[PackageDefinition] = Field(default_factory=list, max_length=100)
    requirements: dict
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExportWorkflowPackage(Contract):
    workflow: Workflow


class ImportWorkflowPackage(Contract):
    package: WorkflowPackage
    model_bindings: dict[str, str] = Field(default_factory=dict)
    tool_bindings: dict[str, str] = Field(default_factory=dict)
    credential_bindings: dict[str, str] = Field(default_factory=dict)
    knowledge_bindings: dict[str, str] = Field(default_factory=dict)
    memory_bindings: dict[str, str] = Field(default_factory=dict)
    allow_development: bool = False
    workflow_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")


def agent_config(arguments):
    return AgentConfig.model_validate(
        {**arguments, "prompt": ""} if isinstance(arguments.get("prompt"), dict) else arguments
    )


def check_credential_literals(url, headers):
    names = [*headers, *(name for name, _ in parse_qsl(urlsplit(url).query))]
    if any(
        re.search(r"authorization|api[-_]?key|secret|token|password|cookie", name, re.I) for name in names
    ):
        raise ValueError("move sensitive headers/query values to credential references before sharing")


def check_api(row):
    if row.kind != "api" or row.revision < 1 or digest(row.body) != row.digest:
        raise ValueError("invalid pinned API definition")
    api = HTTPTool.model_validate(row.body)
    if api.name != row.id or api.api_key:
        raise ValueError("API identity mismatch or inline secret")
    check_credential_literals(api.url, api.headers)
    return api


def inventory(workflow, apis, external_tools, granted_workflows, extensions=None):
    """Derive execution requirements from actual code paths, never trust a package summary."""
    models, tools, knowledge, memory, credentials, origins, writes, grants = (
        {},
        set(),
        set(),
        set(),
        set(),
        set(),
        set(),
        [],
    )
    used_apis, used_grants, visiting = set(), set(), set()
    extensions = extensions or []
    from .extension_contracts import ExtensionPackage

    packages = [ExtensionPackage.model_validate(p) for p in extensions]
    extension_tools = {
        (a.spec.name, p.manifest.revision): a.spec
        for p in packages
        for a in p.manifest.tools + p.manifest.services + p.manifest.commands
    }
    pinned = {p.manifest.id: p.manifest.revision for p in packages}
    if len(pinned) != len(packages):
        raise ValueError("duplicate extension dependency")
    for p in packages:
        if digest(p.model_dump(exclude={"digest", "signature"})) != p.digest:
            raise ValueError("extension dependency digest mismatch")
        if any(pinned.get(name) != rev for name, rev in p.manifest.dependencies.items()):
            raise ValueError("missing pinned extension dependency")
    spec_by_name = {s["name"]: ToolSpec.model_validate(s) for s in external_tools}
    if len(spec_by_name) != len(external_tools):
        raise ValueError("duplicate external tool requirement")

    def tool(name, revision=None, *, external_service=False):
        key = (name, revision)
        if key in extension_tools:
            spec = extension_tools[key]
        elif key in apis:
            used_apis.add(key)
            definition = check_api(apis[key])
            parts = urlsplit(definition.url)
            origins.add(parts.scheme + "://" + parts.netloc)
            if definition.api_key_env:
                credentials.add(definition.api_key_env)
            effect = definition.effect or (
                "read" if definition.method in ("GET", "HEAD", "OPTIONS") else "write"
            )
            spec = ToolSpec(
                name=name,
                input_schema=definition.input_schema,
                output_schema=definition.output_schema,
                effect=effect,
                idempotent=effect == "read" or definition.idempotent,
            )
        else:
            if (revision is not None and not external_service) or name not in spec_by_name:
                raise ValueError("missing tool dependency: " + name)
            tools.add(name)
            spec = spec_by_name[name]
        if spec.effect == "write":
            writes.add(name)
        return spec

    def model(name, capability="chat"):
        if not isinstance(capability, str):
            raise ValueError("sharing requires a literal model capability")
        models.setdefault(name, set()).add(capability)

    def walk(raw, depth=0):
        if depth > 8:
            raise ValueError("workflow package nesting exceeds 8 levels")
        flow = Workflow.model_validate(raw)
        if any(pinned.get(n) != r for n, r in flow.metadata.get("extensions", {}).items()):
            raise ValueError("workflow extension snapshot is missing from package")
        for step in flow.steps:
            if step.kind == "goal":
                raise ValueError("export the goal's saved workflow version, not its controller or execution history")
            if step.workflow_ref:
                raise ValueError(
                    "portable subworkflows must contain a frozen body, without unresolved workflow_ref"
                )
            if step.kind == "tool":
                spec = tool(step.target, step.tool_revision)
                check_literals(step.input, spec.input_schema, step.id)
                if step.target == "memory.search" and isinstance(step.input.get("namespace"), str):
                    memory.add(step.input["namespace"])
            elif step.kind == "model":
                model(step.target, step.input.get("capability", "chat"))
            elif step.kind == "agent":
                config = agent_config(step.input)
                if config.skills or config.skill_access or config.policy:
                    raise ValueError("sharing requires frozen skill and policy instructions")
                model(step.target)
                for name in config.tools:
                    tool(name, config.tool_revisions.get(name))
                knowledge.update(config.knowledge)
                memory.update(config.memory_namespaces)
                if config.development:
                    grant = config.development
                    grants.append(grant.model_dump())
                    for name in grant.tools:
                        tool(name, grant.tool_revisions.get(name))
                    for name in grant.models:
                        model(name)
                    for service in grant.services.values():
                        check_credential_literals(service.url, {})
                        params = re.findall(r"\{([^{}]+)\}", service.url)
                        HTTPTool(
                            name="share.validate",
                            description="Validate development service",
                            method=service.methods[0],
                            input_schema={
                                "type": "object",
                                "properties": {n: {"type": "string"} for n in params},
                                "required": params,
                            },
                            **service.model_dump(exclude={"methods"}),
                        )
                        parts = urlsplit(service.url)
                        origins.add(parts.scheme + "://" + parts.netloc)
                        if service.api_key_env:
                            credentials.add(service.api_key_env)
                    for name, version in grant.workflows.items():
                        key = (name, version)
                        if key not in granted_workflows or key in visiting:
                            raise ValueError("missing or cyclic granted workflow: " + name)
                        used_grants.add(key)
                        visiting.add(key)
                        walk(granted_workflows[key].body, depth + 1)
                        visiting.remove(key)
            elif step.kind == "retrieve":
                namespace = step.input.get("namespace")
                if not isinstance(namespace, str):
                    raise ValueError("sharing requires a literal knowledge namespace")
                knowledge.add(namespace)
            if step.compensate:
                spec = tool(step.compensate["target"], step.compensate.get("tool_revision"))
                check_literals(step.compensate.get("input", {}), spec.input_schema, step.id + ".compensate")
            if step.body:
                walk(step.body, depth + 1)

    walk(workflow)
    for p in packages:
        for name in p.manifest.required_services:
            if "tool:" + name in p.manifest.permissions:
                tool(name, p.manifest.service_versions.get(name), external_service=True)
    if used_apis != set(apis) or tools != set(spec_by_name) or used_grants != set(granted_workflows):
        raise ValueError("package contains dependencies outside the workflow closure")
    return {
        "models": {k: sorted(v) for k, v in sorted(models.items())},
        "external_tools": [spec_by_name[k].model_dump() for k in sorted(tools)],
        "knowledge": sorted(knowledge),
        "memory": sorted(memory),
        "credentials": sorted(credentials),
        "network_origins": sorted(origins),
        "write_tools": sorted(writes),
        "development": grants,
        "data_included": False,
        **({"extension_packages": extensions} if extensions else {}),
    }


def export_workflow(hub, workflow, source=None):
    root = hub.prepare(workflow).model_dump()
    apis, external, granted, visiting = {}, {}, {}, set()
    extensions = {}

    def collect_extension(name, revision):
        p = hub.extensions.packages[name, revision]
        if name in extensions:
            if extensions[name].manifest.revision != revision:
                raise ValueError("sharing multiple extension generations requires separate workflows")
            return
        extensions[name] = p
        for dep, rev in p.manifest.dependencies.items():
            if dep not in extensions:
                collect_extension(dep, rev)
        for service in p.manifest.required_services:
            if "tool:" + service in p.manifest.permissions:
                collect_tool(service, p.manifest.service_versions.get(service))

    def collect_tool(name, revision=None):
        if name in hub.extensions.owners:
            owner = hub.extensions.owners[name]
            revision = revision or hub.extensions.active[owner]
            collect_extension(owner, revision)
            return revision
        if name.startswith("connection.") or name in getattr(hub.tools, "mcp_owners", {}):
            external[name] = hub.tools.spec(name, revision).model_dump()
            return None
        if revision is not None:
            body = hub.development.get("api", name, revision)["definition"]
        elif name in getattr(hub, "http_definitions", {}):
            body, revision = hub.http_definitions[name], 1
        else:
            external[name] = hub.tools.spec(name).model_dump()
            return None
        apis[name, revision] = PackageDefinition(
            kind="api", id=name, revision=revision, body=body, digest=digest(body)
        )
        return revision

    def freeze(flow, depth=0):
        if depth > 8:
            raise ValueError("workflow package nesting exceeds 8 levels")
        refs = {}
        # Voice connections belong to the receiving host, like other external tools.
        flow.get("metadata", {}).pop("voice_settings", None)
        for name, rev in flow.get("metadata", {}).get("extensions", {}).items():
            collect_extension(name, rev)
        for step in flow["steps"]:
            if step.get("workflow_ref"):
                refs[step["id"]] = step.pop("workflow_ref")
            if step["kind"] == "tool":
                step["tool_revision"] = collect_tool(step["target"], step.get("tool_revision"))
            if step["kind"] == "agent":
                config = step["input"]
                revisions = config.setdefault("tool_revisions", {})
                for name in config.get("tools", []):
                    if number := collect_tool(name, revisions.get(name)):
                        revisions[name] = number
                    else:
                        revisions.pop(name, None)
                if grant := config.get("development"):
                    for name in grant["tools"]:
                        if number := collect_tool(name, grant["tool_revisions"].get(name)):
                            grant["tool_revisions"][name] = number
                        else:
                            grant["tool_revisions"].pop(name, None)
                    for name, number in grant["workflows"].items():
                        key = (name, number)
                        if key in visiting:
                            raise ValueError("cyclic development workflow grant")
                        if key not in granted:
                            visiting.add(key)
                            raw = hub.prepare(
                                hub.development.get("workflow", name, number)["workflow"]
                            ).model_dump()
                            freeze(raw, depth + 1)
                            granted[key] = PackageDefinition(
                                kind="workflow", id=name, revision=number, body=raw, digest=digest(raw)
                            )
                            visiting.remove(key)
            if step.get("compensate"):
                c = step["compensate"]
                if number := collect_tool(c["target"], c.get("tool_revision")):
                    c["tool_revision"] = number
                else:
                    c.pop("tool_revision", None)
            if step.get("body"):
                freeze(step["body"], depth + 1)
        if refs:
            flow.setdefault("metadata", {})["shared_source_workflows"] = refs

    freeze(root)
    required = inventory(
        root, apis, list(external.values()), granted, [extensions[k].model_dump() for k in sorted(extensions)]
    )
    # Human-facing model hints never contain provider connection URLs or API keys.
    required["model_hints"] = {}
    for name in required["models"]:
        binding = (
            hub.extensions.provider_binding(name, root["metadata"].get("extensions", {}))
            or hub.models.bindings[name]
        )
        required["model_hints"][name] = {
            "model": binding.model,
            "dialect": getattr(binding.provider, "dialect", None),
        }
    body = {
        "format": "easyagent.workflow-package.v1",
        "source": source or {"id": "draft", "revision": 0},
        "workflow": root,
        "apis": [apis[k].model_dump() for k in sorted(apis)],
        "granted_workflows": [granted[k].model_dump() for k in sorted(granted)],
        "requirements": required,
    }
    result = WorkflowPackage(**body, digest=digest(body))
    if len(result.model_dump_json().encode()) > 1_500_000:
        raise ValueError("workflow package exceeds 1.5 MB; reduce embedded inputs or split the workflow")
    return result


def package_index(package):
    if digest(package.model_dump(exclude={"digest"})) != package.digest:
        raise ValueError("workflow package digest mismatch")
    apis = {(r.id, r.revision): r for r in package.apis}
    granted = {(r.id, r.revision): r for r in package.granted_workflows}
    if len(apis) != len(package.apis) or len(granted) != len(package.granted_workflows):
        raise ValueError("duplicate package dependency")
    for row in package.granted_workflows:
        if row.kind != "workflow" or digest(row.body) != row.digest:
            raise ValueError("invalid granted workflow definition")
    computed = inventory(
        package.workflow,
        apis,
        package.requirements.get("external_tools", []),
        granted,
        package.requirements.get("extension_packages"),
    )
    declared = {k: v for k, v in package.requirements.items() if k != "model_hints"}
    if computed != declared:
        raise ValueError("workflow requirements do not match executable definitions")
    return apis, granted, computed


def preview_import(hub, options):
    request = ImportWorkflowPackage.model_validate(options)
    apis, granted, required = package_index(request.package)
    missing = []
    for raw in required.get("extension_packages", []):
        from .extensions import validate_package

        p = validate_package(raw, hub.extensions.publishers())
        installed = hub.extensions.packages.get((p.manifest.id, p.manifest.revision))
        if installed and installed.digest != p.digest:
            raise Conflict("installed extension has different content: " + p.manifest.id)
        if not installed:
            missing.append("请先检查并安装随包扩展：" + p.manifest.id + "@" + str(p.manifest.revision))
    mappings = {
        "model_bindings": required["models"],
        "tool_bindings": {s["name"] for s in required["external_tools"]},
        "credential_bindings": required["credentials"],
        "knowledge_bindings": required["knowledge"],
        "memory_bindings": required["memory"],
    }
    for field, allowed in mappings.items():
        if set(getattr(request, field)) - set(allowed):
            raise ValueError("unknown binding in " + field)
    for ref, target in request.credential_bindings.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target):
            raise ValueError("credential bindings must be environment variable names")
    for name, caps in required["models"].items():
        target = request.model_bindings.get(name, name)
        bound = hub.models.bindings.get(target)
        if not bound or not set(caps).issubset(bound.capabilities):
            missing.append("模型 " + name + " → " + target + "，需要 " + ",".join(caps))
    for spec in required["external_tools"]:
        target = request.tool_bindings.get(spec["name"], spec["name"])
        if target.startswith("voice.") and not hub.voice.settings():
            missing.append("请先在连接中心配置语音服务")
        try:
            versions = {None}
            for raw in required.get("extension_packages", []):
                m = raw["manifest"]
                if spec["name"] in m["required_services"]:
                    if target != spec["name"]:
                        missing.append("源码扩展固定服务名称，请在接收端按原名称配置：" + spec["name"])
                    versions.add(m.get("service_versions", {}).get(spec["name"]))
            for version in versions:
                local = hub.tools.spec(target, version).model_dump()
                if any(
                    local[k] != spec[k] for k in ("input_schema", "output_schema", "effect", "idempotent")
                ):
                    missing.append("工具协议或权限不匹配：" + target)
        except ValueError:
            missing.append("外部工具尚未安装：" + target)
    namespaces = {d["namespace"] for d in hub.knowledge.list()}
    for name in required["knowledge"]:
        if request.knowledge_bindings.get(name, name) not in namespaces:
            missing.append("知识库未准备：" + name)
    for row in apis.values():
        try:
            existing = hub.development.get("api", row.id, row.revision)
        except KeyError:
            existing = None
        if existing and digest(existing["definition"]) != digest(row.body):
            raise Conflict("existing API version differs: " + row.id + "@" + str(row.revision))
        ref = row.body.get("api_key_env")
        if ref:
            target = request.credential_bindings.get(ref, ref)
            with hub.store.connect() as db:
                bound = db.execute(
                    "SELECT value FROM memory WHERE namespace='credential-bindings' AND key=?",
                    (f"{row.id}@{row.revision}:{ref}",),
                ).fetchone()
            if (
                existing
                and ref in request.credential_bindings
                and (json.loads(bound[0]) if bound else ref) != target
            ):
                raise Conflict("cannot rebind an existing API version: " + row.id)
            available = (
                hub.development.credential(ref, row.id, row.revision)
                if existing and ref not in request.credential_bindings
                else hub.development.credential(target)
            )
            if not available:
                missing.append("缺少凭证环境变量：" + target)
    for grant in required["development"]:
        if not request.allow_development:
            missing.append("需要确认此工作流的运行时开发权限：" + grant["namespace"])
        for service in grant["services"].values():
            key = service.get("api_key_env")
            if key:
                if not hub.development.credential(request.credential_bindings.get(key, key)):
                    missing.append("开发服务缺少凭证环境变量：" + request.credential_bindings.get(key, key))
    return {
        "name": request.package.workflow["name"],
        "source": request.package.source,
        "node_count": count_nodes(request.package.workflow),
        "api_count": len(apis),
        "requirements": required,
        "missing": sorted(set(missing)),
        "ready": not missing,
        "execution_started": False,
        "publisher_authenticated": False,
    }


def count_nodes(workflow):
    return sum(1 + (count_nodes(s["body"]) if s.get("body") else 0) for s in workflow["steps"])


def stage_api_dependencies(hub, options):
    """Stage immutable APIs before granting extension activation; never execute the workflow."""
    request = ImportWorkflowPackage.model_validate(options)
    preview_import(hub, request)
    apis, _, _ = package_index(request.package)
    with hub.store.transaction() as db:
        for row in apis.values():
            existing = db.execute(
                "SELECT body FROM definition_versions WHERE kind='api' AND id=? AND revision=?",
                (row.id, row.revision),
            ).fetchone()
            if existing and digest(json.loads(existing[0])) != digest(row.body):
                raise Conflict("existing API version differs: " + row.id)
            ref = row.body.get("api_key_env")
            if ref:
                bound = db.execute(
                    "SELECT value FROM memory WHERE namespace='credential-bindings' AND key=?",
                    (f"{row.id}@{row.revision}:{ref}",),
                ).fetchone()
                old = json.loads(bound[0]) if bound else ref
                target = request.credential_bindings.get(ref, old if existing else ref)
                if existing and target != old:
                    raise Conflict("cannot rebind an existing API version: " + row.id)
                if not hub.development.credential(target):
                    raise ValueError("请先在连接中心配置凭证：" + target)
                if not existing and target != ref:
                    db.execute(
                        "INSERT INTO memory VALUES('credential-bindings',?,?,?,?)",
                        (f"{row.id}@{row.revision}:{ref}", encode(target), "workflow-package", time.time()),
                    )
            if not existing:
                db.execute(
                    "INSERT INTO definition_versions VALUES('api',?,?,?,NULL,NULL,?)",
                    (row.id, row.revision, encode(row.body), time.time()),
                )
    for row in apis.values():
        hub.development.refresh_api(row.id, row.revision)
    return {
        "staged": [{"id": row.id, "revision": row.revision} for row in apis.values()],
        "execution_started": False,
    }


def import_workflow(hub, options):
    request = ImportWorkflowPackage.model_validate(options)
    preview = preview_import(hub, request)
    if not preview["ready"]:
        raise ValueError("；".join(preview["missing"]))
    apis, granted, _ = package_index(request.package)
    fingerprint = digest(request.model_dump(exclude={"workflow_id"}))[:20]
    identifier = request.workflow_id or "shared." + fingerprint
    grant_ids = {key: "shared." + fingerprint + "." + digest(list(key))[:12] for key in granted}

    def rewrite(raw):
        flow = copy.deepcopy(raw)
        for s in flow["steps"]:
            if s["kind"] in ("model", "agent"):
                s["target"] = request.model_bindings.get(s["target"], s["target"])
            if s["kind"] == "tool":
                original = s["target"]
                s["target"] = request.tool_bindings.get(original, original)
                if original == "memory.search" and isinstance(s["input"].get("namespace"), str):
                    key = s["input"]["namespace"]
                    s["input"]["namespace"] = request.memory_bindings.get(key, key)
            if s["kind"] == "retrieve":
                key = s["input"]["namespace"]
                s["input"]["namespace"] = request.knowledge_bindings.get(key, key)
            if s["kind"] == "agent":
                cfg = s["input"]
                cfg["tools"] = [request.tool_bindings.get(n, n) for n in cfg["tools"]]
                cfg["knowledge"] = [request.knowledge_bindings.get(n, n) for n in cfg["knowledge"]]
                cfg["memory_namespaces"] = [
                    request.memory_bindings.get(n, n) for n in cfg["memory_namespaces"]
                ]
                if grant := cfg.get("development"):
                    grant["tools"] = [request.tool_bindings.get(n, n) for n in grant["tools"]]
                    grant["models"] = [request.model_bindings.get(n, n) for n in grant["models"]]
                    grant["workflows"] = {
                        grant_ids[name, version]: 1 for name, version in grant["workflows"].items()
                    }
                    for service in grant["services"].values():
                        if key := service.get("api_key_env"):
                            service["api_key_env"] = request.credential_bindings.get(key, key)
            if s.get("compensate"):
                name = s["compensate"]["target"]
                s["compensate"]["target"] = request.tool_bindings.get(name, name)
            if s.get("body"):
                s["body"] = rewrite(s["body"])
        return Workflow.model_validate(flow).model_dump()

    workflows = {grant_ids[key]: rewrite(row.body) for key, row in granted.items()}
    root = rewrite(request.package.workflow)
    root["metadata"]["shared_from"] = {
        "source": request.package.source,
        "package_digest": request.package.digest,
    }
    workflows[identifier] = root
    definitions = [("api", row.id, row.revision, row.body) for row in apis.values()]
    definitions += [("workflow", name, 1, body) for name, body in workflows.items()]
    with hub.store.transaction() as db:
        for kind, name, version, body in definitions:
            existing = db.execute(
                "SELECT body FROM definition_versions WHERE kind=? AND id=? AND revision=?",
                (kind, name, version),
            ).fetchone()
            if existing and digest(json.loads(existing[0])) != digest(body):
                raise Conflict("import destination has different content: " + name)
            if kind == "api" and existing and (ref := body.get("api_key_env")) in request.credential_bindings:
                # Another importer can finish after preview; recheck under the write lock.
                binding = db.execute(
                    "SELECT value FROM memory WHERE namespace='credential-bindings' AND key=?",
                    (f"{name}@{version}:{ref}",),
                ).fetchone()
                if (json.loads(binding[0]) if binding else ref) != request.credential_bindings[ref]:
                    raise Conflict("cannot rebind an existing API version: " + name)
            if (
                kind == "workflow"
                and not existing
                and db.execute(
                    "SELECT 1 FROM memory WHERE namespace='studio-workflows' AND key=?", (name,)
                ).fetchone()
            ):
                raise Conflict("workflow destination already exists: " + name)
            if not existing:
                db.execute(
                    "INSERT INTO definition_versions VALUES(?,?,?,?,NULL,NULL,?)",
                    (kind, name, version, encode(body), time.time()),
                )
        for name, body in workflows.items():
            latest = db.execute(
                "SELECT body FROM definition_versions WHERE kind='workflow' AND id=? ORDER BY revision DESC LIMIT 1",
                (name,),
            ).fetchone()[0]
            db.execute(
                "INSERT OR REPLACE INTO memory VALUES('studio-workflows',?,?,?,?)",
                (name, latest, "workflow-package", time.time()),
            )
        for row in apis.values():
            ref = row.body.get("api_key_env")
            if ref in request.credential_bindings:
                db.execute(
                    "INSERT OR REPLACE INTO memory VALUES('credential-bindings',?,?,?,?)",
                    (
                        f"{row.id}@{row.revision}:{ref}",
                        encode(request.credential_bindings[ref]),
                        "workflow-package",
                        time.time(),
                    ),
                )
        db.execute(
            "INSERT OR REPLACE INTO memory VALUES('workflow-imports',?,?,?,?)",
            (
                identifier,
                encode({"digest": request.package.digest, "unsigned": True}),
                "workflow-package",
                time.time(),
            ),
        )
    for row in apis.values():
        hub.development.refresh_api(row.id, row.revision)
    return {**preview, "imported": True, "id": identifier, "revision": 1, "workflow": root}


def install_workflow_packages(app, hub):
    from fastapi import Response
    @app.post("/v1/workflow-packages/export")
    async def export(body: ExportWorkflowPackage):
        package = export_workflow(hub, body.workflow)
        hub.store.memory_put("workflow-exports", package.digest, package.model_dump(), "local-export")
        return package

    @app.get("/v1/workflow-packages/exports/{identifier}")
    async def exported(identifier: str):
        with hub.store.connect() as db:
            row = db.execute(
                "SELECT value FROM memory WHERE namespace='workflow-exports' AND key=?", (identifier,)
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return Response(
            row[0],
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="workflow.eah-workflow.json"'},
        )

    @app.get("/v1/studio/workflows/{identifier}/package")
    async def download(identifier: str, revision: int | None = None):
        source = hub.development.get("workflow", identifier, revision)
        package = export_workflow(hub, source["workflow"], {"id": identifier, "revision": source["revision"]})
        return Response(
            package.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="workflow.eah-workflow.json"'},
        )

    @app.post("/v1/workflow-packages/preview")
    async def preview(body: ImportWorkflowPackage):
        return preview_import(hub, body)

    @app.post("/v1/workflow-packages/import", status_code=201)
    async def imported(body: ImportWorkflowPackage):
        return import_workflow(hub, body)

    @app.post("/v1/workflow-packages/dependencies", status_code=201)
    async def dependencies(body: ImportWorkflowPackage):
        return stage_api_dependencies(hub, body)
