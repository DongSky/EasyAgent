import copy
import json
import os

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.api import create_app
from easyagent.components import digest
from easyagent.contracts import Policy
from easyagent.http_tools import register_http_tool
from easyagent.models import HTTPProvider, MockProvider
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from easyagent.workflow_packages import export_workflow, import_workflow, preview_import
from test_interop import subprocess_output


def rehash(package):
    package["digest"] = digest({k: v for k, v in package.items() if k != "digest"})
    return package


async def test_share_complete_agent_workflow_js_import_clean_hub_and_restart(api, tmp_path, monkeypatch):
    url, source = api
    remote = FastAPI()
    calls = []

    @remote.get("/lookup")
    async def lookup(q: str, request: Request):
        calls.append(("lookup", request.headers["authorization"]))
        return {"text": q}

    @remote.post("/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        assert body["model"] == "fixture-model"
        calls.append(("model", request.headers["authorization"]))
        messages = body["messages"]
        if body.get("tools") and not any(m["role"] == "tool" for m in messages):
            assert any(
                "FROZEN_SKILL" in m.get("content", "") and "FROZEN_POLICY" in m.get("content", "")
                for m in messages
            )
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "type": "function",
                                    "function": {
                                        "name": body["tools"][0]["function"]["name"],
                                        "arguments": json.dumps({"q": "agent checked"}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}

    monkeypatch.setenv("SHARE_SOURCE_KEY", "source-secret")
    monkeypatch.setenv("SHARE_TARGET_KEY", "target-secret")
    async with live_server(remote) as base, httpx.AsyncClient(base_url=url) as client:
        source.models.register(
            "source-model", HTTPProvider(base, "source-model-key"), "fixture-model", ["chat"]
        )
        # Configured APIs without a database revision are captured as portable definitions too.
        register_http_tool(
            source,
            {
                "name": "share.lookup",
                "description": "Lookup",
                "url": base + "/lookup",
                "api_key_env": "SHARE_SOURCE_KEY",
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        )
        root = tmp_path / "skills" / "test-skill"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: test-skill\ndescription: test\n---\nFROZEN_SKILL")
        source.skills.discover(root.parent)
        policy = source.evolution.propose(
            Policy(
                name="source-policy",
                model="source-model",
                instructions="FROZEN_POLICY",
                tools=["share.lookup"],
            )
        )
        with source.store.connect() as db:
            db.execute("UPDATE policies SET status='active' WHERE id=?", (policy["id"],))
        source.development.save_workflow(
            "share.child",
            {
                "name": "Pinned lookup",
                "steps": [
                    {"id": "lookup", "target": "share.lookup", "input": {"q": {"$ref": "$input.query"}}}
                ],
            },
        )
        workflow = {
            "name": "Complete share acceptance",
            "inputs": {"query": "service status"},
            "metadata": {"canvas": {"positions": {"query": {"x": 20, "y": 40}}}},
            "steps": [
                {
                    "id": "query",
                    "kind": "subworkflow",
                    "workflow_ref": {"id": "share.child", "revision": 1},
                    "input": {"query": {"$ref": "$input.query"}},
                },
                {
                    "id": "model",
                    "kind": "model",
                    "target": "source-model",
                    "depends_on": ["query"],
                    "input": {"prompt": {"$ref": "query.results.0.lookup.text"}},
                },
                {
                    "id": "agent",
                    "kind": "agent",
                    "target": "source-model",
                    "depends_on": ["model"],
                    "input": {
                        "prompt": {"$ref": "model.text"},
                        "tools": ["share.lookup"],
                        "skills": ["test-skill"],
                        "policy": "source-policy",
                    },
                },
                {
                    "id": "input",
                    "kind": "input",
                    "depends_on": ["agent"],
                    "input": {
                        "prompt": "确认完成标签",
                        "schema": {
                            "type": "object",
                            "properties": {"label": {"type": "string"}},
                            "required": ["label"],
                        },
                    },
                },
                {
                    "id": "receipt",
                    "kind": "artifact",
                    "depends_on": ["input"],
                    "when": {"source": "input.label", "equals": "accepted"},
                    "input": {"name": "shared.txt", "content": {"$ref": "input.label"}},
                },
            ],
        }
        saved = await client.post("/v1/studio/workflows", json=workflow)
        assert saved.status_code == 201, saved.text
        identifier = saved.json()["id"]
        package_response = await client.get("/v1/studio/workflows/" + identifier + "/package")
        assert package_response.status_code == 200, package_response.text
        package = package_response.json()
        assert not calls
        assert (
            "source-secret" not in package_response.text and "source-model-key" not in package_response.text
        )
        assert package["workflow"]["metadata"]["canvas"] == workflow["metadata"]["canvas"]
        assert not package["workflow"]["steps"][0].get("workflow_ref")
        assert "FROZEN_SKILL" in package["workflow"]["steps"][2]["input"]["instructions"]
        target = Hub(tmp_path / "destination.db", poll_seconds=0.01)
        target.models.register(
            "local-model", HTTPProvider(base, "destination-model-key"), "fixture-model", ["chat"]
        )
        app = create_app(target)
        install_studio(app, target)
        package_file = tmp_path / "workflow.json"
        package_file.write_text(json.dumps(package))
        async with live_server(app) as target_url, httpx.AsyncClient(base_url=target_url) as dest:
            initial = await dest.post("/v1/workflow-packages/preview", json={"package": package})
            assert initial.status_code == 200 and not initial.json()["ready"]
            assert not calls
            script = """import {readFile} from 'node:fs/promises';
import {HubClient} from './sdk/javascript/index.js';
const c=new HubClient(process.env.EAH_URL);
const p=JSON.parse(await readFile(process.env.EAH_PACKAGE_FILE,'utf8'));
console.log(JSON.stringify(await c.importWorkflow(p,{model_bindings:{'source-model':'local-model'},credential_bindings:{SHARE_SOURCE_KEY:'SHARE_TARGET_KEY'}})));
"""
            result = json.loads(
                await subprocess_output(
                    ["node", "--input-type=module", "-e", script],
                    {**os.environ, "EAH_URL": target_url, "EAH_PACKAGE_FILE": str(package_file)},
                )
            )
            assert result["imported"] and result["ready"] and not result["execution_started"] and not calls
            conflict = await dest.post(
                "/v1/workflow-packages/import",
                json={
                    "package": package,
                    "model_bindings": {"source-model": "local-model"},
                    "credential_bindings": {"SHARE_SOURCE_KEY": "SHARE_SOURCE_KEY"},
                },
            )
            assert conflict.status_code == 409, conflict.text
            imported = result["workflow"]
            assert imported["steps"][1]["target"] == "local-model"
            assert (
                imported["steps"][2]["input"]["skills"] == []
                and imported["steps"][2]["input"]["policy"] is None
            )
            run = await target.wait(target.submit(imported))
            assert run["status"] == "waiting_input", run
            await dest.post("/v1/inputs/" + run["input_requests"][0]["id"], json={"label": "accepted"})
            done = await target.wait(run["id"])
            assert done["status"] == "succeeded", done
            info, data = target.artifacts.get(done["steps"][-1]["output"]["id"])
            assert data == b"accepted"
            assert ("lookup", "Bearer target-secret") in calls and (
                "model",
                "Bearer destination-model-key",
            ) in calls
            assert all("source-secret" not in value for _, value in calls)
        restored = Hub(tmp_path / "destination.db", poll_seconds=0.01)
        restored.models.register(
            "local-model", HTTPProvider(base, "destination-model-key"), "fixture-model", ["chat"]
        )
        await restored.start()
        try:
            again = await restored.wait(
                restored.submit(restored.development.get("workflow", result["id"])["workflow"])
            )
            assert again["status"] == "waiting_input", again
            restored.store.cancel(again["id"])
        finally:
            await restored.stop()


async def test_workflow_package_tampering_conflicts_and_bindings_atomic(hub, tmp_path):
    source = {
        "name": "Portable",
        "steps": [{"id": "echo", "target": "core.echo", "input": {"text": "hello"}}],
    }
    package = export_workflow(hub, source).model_dump()
    target = Hub(tmp_path / "target.db")
    for mutation in ["digest", "missing_tool", "extra_api", "effect"]:
        bad = copy.deepcopy(package)
        if mutation == "digest":
            bad["workflow"]["name"] = "edited"
        elif mutation == "missing_tool":
            bad["requirements"]["external_tools"] = []
            rehash(bad)
        elif mutation == "extra_api":
            body = {"name": "unused.api", "url": "http://127.0.0.1:1/", "description": "unused"}
            bad["apis"].append(
                {"kind": "api", "id": "unused.api", "revision": 1, "body": body, "digest": digest(body)}
            )
            rehash(bad)
        else:
            bad["requirements"]["external_tools"][0]["effect"] = "write"
            rehash(bad)
        with pytest.raises(ValueError):
            import_workflow(target, {"package": bad})
        assert not target.development.list_versions("workflow") and not target.development.list_versions(
            "api"
        )
    first = import_workflow(target, {"package": package, "workflow_id": "imported.flow"})
    second = import_workflow(target, {"package": package, "workflow_id": "imported.flow"})
    assert first["id"] == second["id"] and len(target.development.list_versions("workflow")) == 1
    changed = export_workflow(hub, {**source, "name": "Another"}).model_dump()
    from easyagent.store import Conflict

    with pytest.raises(Conflict):
        import_workflow(target, {"package": changed, "workflow_id": "imported.flow"})
    assert target.development.get("workflow", "imported.flow")["workflow"]["name"] == "Portable"


async def test_sharing_data_requirements_and_explicit_development_permission(hub, tmp_path):
    await hub.knowledge.ingest("source-data", "Notes", "notes", "A useful policy")
    source = {
        "name": "Data and development",
        "steps": [
            {"id": "search", "kind": "retrieve", "input": {"namespace": "source-data", "query": "policy"}},
            {
                "id": "agent",
                "kind": "agent",
                "target": "mock",
                "input": {
                    "prompt": "hello",
                    "memory_namespaces": ["source-memory"],
                    "development": {"namespace": "sharedev", "models": ["mock"]},
                },
            },
        ],
    }
    package = export_workflow(hub, source).model_dump()
    clean = Hub(tmp_path / "data.db", poll_seconds=0.01)
    unsafe = copy.deepcopy(source)
    unsafe["steps"][1]["input"]["development"]["services"] = {
        "private": {"url": "https://example.com/api?api_key=not-for-sharing", "methods": ["GET"]}
    }
    with pytest.raises(ValueError, match="credential references"):
        export_workflow(hub, unsafe)
    preview = preview_import(clean, {"package": package})
    assert not preview["ready"] and len(preview["missing"]) == 2
    await clean.knowledge.ingest("local-data", "Local", "notes", "A useful policy")
    clean.models.register("local", MockProvider(), "fixture", ["chat"])
    options = {
        "package": package,
        "allow_development": True,
        "knowledge_bindings": {"source-data": "local-data"},
        "memory_bindings": {"source-memory": "local-memory"},
        "model_bindings": {"mock": "local"},
    }
    imported = import_workflow(clean, options)
    await clean.start()
    try:
        run = await clean.wait(clean.submit(imported["workflow"]))
        assert run["status"] == "succeeded", run
        assert run["steps"][0]["output"]["citations"]
    finally:
        await clean.stop()


async def test_shared_write_and_compensation_keep_approval_and_revision(hub, tmp_path):
    remote = FastAPI()
    calls = []

    @remote.post("/write")
    async def write(request: Request):
        calls.append(await request.json())
        return {"ok": True}

    async with live_server(remote) as endpoint:
        hub.development.save_api(
            {
                "name": "share.write",
                "description": "external write",
                "url": endpoint + "/write",
                "method": "POST",
            }
        )
        source = {
            "name": "Write then undo registration",
            "steps": [
                {"id": "write", "target": "share.write", "compensate": {"target": "share.write", "input": {}}}
            ],
        }
        package = export_workflow(hub, source).model_dump()
        assert len(package["apis"]) == 1 and package["requirements"]["write_tools"] == ["share.write"]
        target = Hub(tmp_path / "write.db", poll_seconds=0.01)
        imported = import_workflow(target, {"package": package})
        assert imported["workflow"]["steps"][0]["compensate"]["tool_revision"] == 1
        assert not calls
        await target.start()
        try:
            run = await target.wait(target.submit(imported["workflow"]))
            assert run["status"] == "waiting_approval" and not calls
            target.tools.approve(target.store, run["approvals"][0]["id"], True)
            result = await target.wait(run["id"])
            assert result["status"] == "succeeded" and len(calls) == 1
        finally:
            await target.stop()
