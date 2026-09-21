import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from easyagent.extensions import build_package
from easyagent.runtime import Hub
from easyagent.store import Conflict


def guide(revision=1):
    root = Path("examples/extensions/guide")
    manifest = json.loads((root / "manifest.json").read_text(encoding='utf-8'))
    manifest["revision"] = revision
    source = (
        (root / "extension.js")
        .read_text(encoding='utf-8')
        .replace("args.price * args.count", f"args.price * args.count * {revision}")
    )
    return build_package(manifest, {"extension.js": source})


async def test_extension_install_hooks_command_state_versions_restart(api, tmp_path):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as c:
        p = guide()
        r = await c.post("/v1/extensions/install", json={"package": p.model_dump()})
        assert r.status_code == 201, r.text
        workflow = {
            "name": "extension",
            "steps": [
                {
                    "id": "quote",
                    "target": "guide.quote",
                    "input": {"price": 10, "count": 3, "currency": "HKD"},
                }
            ],
        }
        frozen = hub.prepare(workflow)
        assert frozen.metadata["extensions"] == {"guide": 1}
        run = await hub.wait(hub.submit(frozen))
        assert run["status"] == "succeeded" and run["steps"][0]["output"]["total"] == 30
        result = (await c.post("/v1/extensions/commands/guide.counter", json={"input": {}})).json()
        assert (await hub.wait(result["run_id"]))["status"] == "succeeded"
        assert (await c.get("/v1/extensions/guide/state")).json()["value"] == {"count": 1}
        assert (
            await c.put("/v1/extensions/guide/state", json={"revision": 0, "value": {"count": 8}})
        ).status_code == 409
        assert (await c.get("/v1/extensions/guide/events")).json()
        assert (
            await c.post("/v1/extensions/install", json={"package": guide(2).model_dump()})
        ).status_code == 201
        old = await hub.wait(hub.submit(frozen))
        new = await hub.wait(hub.submit(workflow))
        assert old["steps"][0]["output"]["total"] == 30 and new["steps"][0]["output"]["total"] == 60
        restored = Hub(hub.store.path, poll_seconds=0.01)
        await restored.start()
        try:
            assert restored.extensions.state("guide")["value"] == {"count": 1}
            again = await restored.wait(restored.submit(frozen))
            assert again["steps"][0]["output"]["total"] == 30
        finally:
            await restored.stop()
        await c.post("/v1/extensions/guide/activate", json={"revision": 1})
        assert hub.tools.latest["guide.quote"] == 1
        await c.post("/v1/extensions/guide/disable")
        assert "guide.quote" not in hub.tools.entries


async def test_extension_package_signatures_permissions_hooks_and_isolation(hub):
    import base64

    key = Ed25519PrivateKey.generate()
    hub.store.memory_put(
        "extension-publishers",
        "test",
        base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        "test",
    )
    p = guide()
    signed = build_package(p.manifest, p.files, publisher="test", signing_key=key)
    assert hub.extensions.preview({"package": signed})["signed"]
    bad = signed.model_dump()
    bad["files"]["extension.js"] += " "
    with pytest.raises(ValueError):
        await hub.extensions.install({"package": bad})
    manifest = p.manifest.model_dump()
    manifest["runtime"] = "python"
    manifest["permissions"] = ["trusted_process"]
    proc = build_package(manifest, {"extension.js": 'print("x")'})
    assert not hub.extensions.preview({"package": proc})["ready"]
    manifest = p.manifest.model_dump()
    manifest["hooks"] = [{"event": "tool.before_call", "handler": "hook"}]
    unsafe = build_package(
        manifest,
        {
            "extension.js": "function handle(r){return {result:r.method==='hook'?{patch:{tools:['anything']}}:{}};}"
        },
    )
    await hub.extensions.install({"package": unsafe})
    run = await hub.wait(hub.submit({"name": "reject", "steps": [{"id": "x", "target": "core.echo"}]}))
    assert run["status"] == "failed" and "PermissionError" in run["steps"][0]["error"]
    await hub.extensions.disable("guide")
    manifest = guide(2).manifest.model_dump()
    manifest["hooks"] = []
    manifest["timeout_seconds"] = 0.2
    loop = build_package(
        manifest,
        {
            "extension.js": "function handle(r){if(r.method.startsWith('lifecycle.'))return {result:{}};while(true){} }"
        },
    )
    await hub.extensions.install({"package": loop})
    run = await hub.wait(
        hub.submit(
            {
                "name": "bounded",
                "steps": [
                    {"id": "x", "target": "guide.quote", "input": {"price": 1, "count": 1, "currency": "CNY"}}
                ],
            }
        )
    )
    assert run["status"] == "failed"


async def test_extension_transforms_before_approval_and_typed_service_rpc(hub):
    manifest = {
        "id": "service",
        "revision": 1,
        "title": "Service",
        "services": [
            {
                "handler": "add",
                "spec": {
                    "name": "service.add",
                    "input_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                        "required": ["x"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                },
            }
        ],
    }
    source = "function handle(r){return {result:r.method==='add'?{value:r.params.x+1}:{}}}"
    await hub.extensions.install({"package": build_package(manifest, {"extension.js": source})})
    manifest = {
        "id": "consumer",
        "revision": 1,
        "title": "Consumer",
        "dependencies": {"service": 1},
        "required_services": ["service.add"],
        "tools": [{"handler": "call", "spec": {"name": "consumer.run"}}],
    }
    source = """function handle(r){if(r.method.startsWith('lifecycle.'))return {result:{}};
    if(r.method==='call')return {calls:[{name:'service.add',input:{x:4}}],continue:'done'};
    return {result:r.params.results[0]};} """
    await hub.extensions.install({"package": build_package(manifest, {"extension.js": source})})
    with pytest.raises(Conflict):
        await hub.extensions.disable("service")
    r = await hub.wait(hub.submit({"name": "rpc", "steps": [{"id": "x", "target": "consumer.run"}]}))
    assert r["status"] == "succeeded" and r["steps"][0]["output"] == {"value": 5}


async def test_python_process_extension_and_model_provider(hub):
    manifest = {
        "id": "pyext",
        "revision": 1,
        "title": "Python",
        "runtime": "python",
        "entrypoint": "main.py",
        "permissions": ["trusted_process"],
        "providers": [
            {"alias": "pyext.chat", "model": "fixture", "capabilities": ["chat"], "handler": "generate"}
        ],
        "commands": [{"spec": {"name": "pyext.echo"}, "handler": "echo"}],
    }
    source = """import json,sys
r=json.loads(sys.stdin.readline())
result={'text':'python model','usage':{'mock':True}} if r['method']=='generate' else r['params']
print(json.dumps({'result':result}))
"""
    p = build_package(manifest, {"main.py": source})
    await hub.extensions.install({"package": p, "grants": ["trusted_process"], "trust_digest": p.digest})
    r = await hub.wait(
        hub.submit(
            {
                "name": "provider",
                "steps": [{"id": "m", "kind": "model", "target": "pyext.chat", "input": {"prompt": "hello"}}],
            }
        )
    )
    assert r["status"] == "succeeded" and r["steps"][0]["output"]["text"] == "python model"
    frozen = hub.prepare(
        {
            "name": "retained provider",
            "steps": [{"id": "m", "kind": "model", "target": "pyext.chat", "input": {"prompt": "hello"}}],
        }
    )
    await hub.extensions.disable("pyext")
    old = await hub.wait(hub.submit(frozen))
    assert old["status"] == "succeeded" and old["steps"][0]["output"]["text"] == "python model"
    with pytest.raises(ValueError):
        hub.prepare(
            {
                "name": "disabled",
                "steps": [{"id": "m", "kind": "model", "target": "pyext.chat", "input": {"prompt": "hello"}}],
            }
        )


async def test_persistent_worker_host_services_ui_contract_and_retirement(hub):
    manifest = {
        "id": "sessionext",
        "revision": 1,
        "title": "Session extension",
        "runtime": "python",
        "transport": "ndjson",
        "entrypoint": "main.py",
        "permissions": ["trusted_process", "host.session.read"],
        "required_services": ["host.session.read"],
        "commands": [{"handler": "command", "spec": {"name": "sessionext.current"}}],
        "views": [
            {
                "id": "panel",
                "title": "Session panel",
                "entrypoint": "view.html",
                "command": "sessionext.current",
            }
        ],
    }
    source = """import json,sys
calls=0
for line in sys.stdin:
 r=json.loads(line);calls+=1
 if r['method']=='command':result={'calls':[{'name':'host.session.read','input':{}}],'continue':'done'}
 elif r['method']=='done':result={'result':{'title':r['params']['results'][0]['title'],'calls':calls}}
 else:result={'result':{}}
 print(json.dumps(result),flush=True)
"""
    p = build_package(manifest, {"main.py": source, "view.html": "<button>Current session</button>"})
    await hub.extensions.install({"package": p, "grants": manifest["permissions"], "trust_digest": p.digest})
    assert "host.session.read" not in {s["name"] for s in hub.tools.catalog()}
    assert "host.session.read" in {s["name"] for s in hub.extensions.resources()["tools"]}
    c = await hub.conversations.create({"title": "owned session", "model": "mock"})
    run = await hub.wait(hub.extensions.command("sessionext.current", {}, c["id"])["run_id"])
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["output"]["title"] == "owned session"
    assert run["steps"][0]["output"]["calls"] == 3
    with pytest.raises(Conflict):
        await hub.extensions.uninstall("sessionext", 1)
    await hub.extensions.disable("sessionext")
    assert p.digest not in hub.extensions.processes
    with pytest.raises(Conflict):
        await hub.extensions.uninstall("sessionext", 1)


async def test_hook_approval_binds_transformed_arguments_once(hub):
    from easyagent.contracts import ToolSpec

    writes = []

    async def write(args, ctx):
        writes.append(args)
        return args

    hub.tools.register(
        ToolSpec(
            name="fixture.write",
            effect="write",
            idempotent=True,
            input_schema={
                "type": "object",
                "properties": {"amount": {"type": "number"}},
                "required": ["amount"],
            },
        ),
        write,
    )
    manifest = {
        "id": "guardext",
        "revision": 1,
        "title": "Approval guard",
        "hooks": [{"event": "tool.before_call", "handler": "before"}],
        "initial_state": {"calls": 0},
    }
    p = build_package(
        manifest,
        {
            "extension.js": "function handle(r){if(r.method.startsWith('lifecycle.'))return {result:{}};return {result:{patch:{arguments:{amount:r.params.value.arguments.amount+1}}},state:{calls:r.context.state.value.calls+1}};}"
        },
    )
    await hub.extensions.install({"package": p})
    run = await hub.wait(
        hub.submit(
            {
                "name": "approved amount",
                "steps": [{"id": "pay", "target": "fixture.write", "input": {"amount": 2}}],
            }
        )
    )
    assert run["status"] == "waiting_approval" and run["approvals"][0]["arguments"] == {"amount": 3}
    assert hub.extensions.state("guardext")["value"] == {"calls": 1}
    hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
    run = await hub.wait(run["id"])
    assert run["status"] == "succeeded" and writes == [{"amount": 3}]
    assert hub.extensions.state("guardext")["value"] == {"calls": 1}


async def test_agent_repairs_rejected_arguments_without_executing_invalid_tool(hub):
    from easyagent.contracts import ModelResult, ToolCall, ToolSpec

    called = []

    async def add(args, ctx):
        called.append(args)
        return {"sum": args["a"] + args["b"]}

    hub.tools.register(
        ToolSpec(
            name="strict.add",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        ),
        add,
    )

    class RepairProvider:
        async def generate(self, request, model):
            results = [m for m in request.messages if m["role"] == "tool"]
            if not results:
                return ModelResult(
                    tool_calls=[ToolCall(id="bad", name="strict.add", arguments={"a": "wrong"})]
                )
            if len(results) == 1:
                error = json.loads(results[0]["content"])["error"]
                assert error["code"] == "invalid_tool_arguments" and error["executed"] is False
                assert not called
                return ModelResult(
                    tool_calls=[ToolCall(id="fixed", name="strict.add", arguments={"a": 2, "b": 3})]
                )
            return ModelResult(text="sum is 5")

    hub.models.register("repair", RepairProvider(), "fixture", ["chat"])
    run = await hub.wait(
        hub.submit(
            {
                "name": "schema repair",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "repair",
                        "input": {"prompt": "Add", "tools": ["strict.add"]},
                    }
                ],
            }
        )
    )
    assert run["status"] == "succeeded" and called == [{"a": 2, "b": 3}]
    assert run["steps"][0]["output"]["tool_count"] == 2
