import json
import pytest
from easyagent.extensions import build_package
from easyagent.extension_cli import scaffold, package_directory
from easyagent.runtime import Hub
from easyagent.workflow_packages import export_workflow, import_workflow, preview_import


async def test_source_extension_languages_upgrade_migration_and_workflow_sharing(hub, tmp_path):
    for language in ("node", "rust"):
        root = tmp_path / language
        scaffold(root, "example_" + language, language)
        p = package_directory(root)
        await hub.extensions.install({"package": p, "grants": ["trusted_process"], "trust_digest": p.digest})
        r = await hub.wait(
            hub.submit(
                {
                    "name": language,
                    "steps": [
                        {"id": "echo", "target": p.manifest.id + ".echo", "input": {"language": language}}
                    ],
                }
            ),
            timeout=120,
        )
        assert r["status"] == "succeeded" and r["steps"][0]["output"] == {"language": language}
    p = build_package(
        {
            "id": "states",
            "title": "State",
            "revision": 1,
            "state_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
            "initial_state": {"count": 3},
            "tools": [{"handler": "state", "spec": {"name": "states.read"}}],
        },
        {"extension.js": "function handle(r){return {result:r.method==='state'?r.context.state.value:{}};}"},
    )
    await hub.extensions.install({"package": p})
    frozen = hub.prepare({"name": "original", "steps": [{"id": "state", "target": "states.read"}]})
    m = p.manifest.model_dump()
    m.update(
        revision=2,
        migrations={1: "migrate"},
        state_schema={
            "type": "object",
            "properties": {"total": {"type": "integer"}},
            "required": ["total"],
            "additionalProperties": False,
        },
        initial_state={"total": 0},
    )
    newer = build_package(
        m,
        {
            "extension.js": "function handle(r){if(r.method==='migrate')return {result:{state:{total:r.params.state.count+4},settings:r.params.settings}};return {result:r.method==='state'?r.context.state.value:{}};}"
        },
    )
    await hub.extensions.install({"package": newer})
    r = await hub.wait(hub.submit(frozen))
    assert r["steps"][0]["output"] == {"count": 3}
    r = await hub.wait(hub.submit({"name": "new", "steps": [{"id": "state", "target": "states.read"}]}))
    assert r["steps"][0]["output"] == {"total": 7}
    await hub.extensions.activate("states", 1)
    assert hub.extensions.state("states")["value"] == {"count": 3}
    await hub.extensions.activate("states", 2)
    assert hub.extensions.state("states")["value"] == {"total": 7}
    package = export_workflow(hub, frozen)
    other = Hub(tmp_path / "clean.db", poll_seconds=0.01)
    await other.start()
    try:
        assert not preview_import(other, {"package": package})["ready"]
        for raw in package.requirements["extension_packages"]:
            from easyagent.extension_contracts import ExtensionPackage

            dep = ExtensionPackage.model_validate(raw)
            await other.extensions.install(
                {"package": dep, "grants": dep.manifest.permissions, "trust_digest": dep.digest}
            )
        imported = import_workflow(other, {"package": package})
        result = await other.wait(other.submit(imported["workflow"]))
        assert result["status"] == "succeeded" and result["steps"][0]["output"] == {"count": 3}
    finally:
        await other.stop()


async def test_generated_js_and_wasm_test_publish_reuse_limits(hub):
    manifest = {
        "id": "generated_sum",
        "revision": 1,
        "title": "Sum",
        "tools": [{"handler": "sum", "spec": {"name": "generated_sum.add"}}],
    }
    candidate = hub.code.propose(
        {
            "manifest": manifest,
            "files": {
                "extension.js": "function handle(r){return {result:r.method==='sum'?{sum:r.params.a+r.params.b}:{}};}"
            },
            "scenarios": [{"tool": "generated_sum.add", "input": {"a": 2, "b": 5}, "expected": {"sum": 7}}],
        }
    )
    with pytest.raises(Exception):
        await hub.code.publish(candidate["id"])
    assert (await hub.code.test(candidate["id"]))["passed"]
    await hub.code.publish(candidate["id"])
    incorrect = hub.code.propose(
        {
            "manifest": {**manifest, "revision": 2},
            "files": {"extension.js": "function handle(r){return {result:{sum:999}};}"},
            "scenarios": [{"tool": "generated_sum.add", "input": {"a": 2, "b": 5}, "expected": {"sum": 7}}],
        }
    )
    assert not (await hub.code.test(incorrect["id"]))["passed"]
    with pytest.raises(Exception):
        await hub.code.publish(incorrect["id"])
    for title in ["budget", "travel"]:
        r = await hub.wait(
            hub.submit(
                {
                    "name": title,
                    "steps": [{"id": "sum", "target": "generated_sum.add", "input": {"a": 3, "b": 4}}],
                }
            )
        )
        assert r["status"] == "succeeded" and r["steps"][0]["output"] == {"sum": 7}
    value = json.dumps({"result": {"value": 42}}, separators=(",", ":"))
    escaped = "".join("\\" + format(b, "02x") for b in value.encode())
    wat = f'(module (memory (export "memory") 1 1) (data (i32.const 0) "{escaped}") (func (export "alloc") (param i32) (result i32) i32.const 4096) (func (export "handle") (param i32 i32) (result i64) i64.const {len(value)}))'
    p = build_package(
        {
            "id": "wasm_demo",
            "revision": 1,
            "title": "Pure WASM",
            "runtime": "wasm",
            "entrypoint": "main.wat",
            "tools": [{"handler": "main", "spec": {"name": "wasm_demo.answer"}}],
        },
        {"main.wat": wat},
    )
    await hub.extensions.install({"package": p})
    r = await hub.wait(
        hub.submit({"name": "wasm", "steps": [{"id": "answer", "target": "wasm_demo.answer"}]})
    )
    assert r["steps"][0]["output"] == {"value": 42}
    bad = build_package(
        {**p.manifest.model_dump(), "id": "wasm_bad", "tools": []},
        {"main.wat": '(module (import "env" "read_file" (func)))'},
    )
    with pytest.raises(ValueError):
        await hub.extensions.install({"package": bad})


async def test_shared_extension_service_closure_and_connector_rebinding(hub, tmp_path):
    from conftest import live_server
    from fastapi import FastAPI
    from easyagent.connections import Connector

    remote = FastAPI()

    @remote.get("/value")
    async def value():
        return {"value": 17}

    async with live_server(remote) as url:
        hub.development.save_api(
            {
                "name": "portable.lookup",
                "description": "Lookup",
                "url": url + "/value",
                "input_schema": {"type": "object", "additionalProperties": False},
            },
            0,
        )
        p = build_package(
            {
                "id": "portable",
                "title": "Portable service",
                "revision": 1,
                "permissions": ["tool:portable.lookup"],
                "required_services": ["portable.lookup"],
                "service_versions": {"portable.lookup": 1},
                "tools": [{"handler": "call", "spec": {"name": "portable.run"}}],
            },
            {
                "extension.js": """function handle(r){
          if(r.method.startsWith('lifecycle.'))return {result:{}};
          if(r.method==='call')return {calls:[{name:'portable.lookup',input:{}}],continue:'done'};
          return {result:r.params.results[0]};} """
            },
        )
        await hub.extensions.install({"package": p, "grants": p.manifest.permissions})
        flow = {"name": "portable service", "steps": [{"id": "x", "target": "portable.run"}]}
        package = export_workflow(hub, flow)
        assert [a.id for a in package.apis] == ["portable.lookup"]
        other = Hub(tmp_path / "portable.db", poll_seconds=0.01)
        await other.start()
        try:
            from easyagent.workflow_packages import stage_api_dependencies

            staged = stage_api_dependencies(other, {"package": package})
            assert staged["execution_started"] is False
            await other.extensions.install({"package": p, "grants": p.manifest.permissions})
            imported = import_workflow(other, {"package": package})
            result = await other.wait(other.submit(imported["workflow"]))
            assert result["status"] == "succeeded" and result["steps"][0]["output"] == {"value": 17}
        finally:
            await other.stop()
        hub.connections.save(Connector(id="example", title="Example", kind="webhook", url=url + "/notify"))
        config = {"prompt": "Do not send anything", "tools": ["connection.example"]}
        exported = export_workflow(
            hub,
            {
                "name": "connector agent",
                "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": config}],
            },
        )
        assert "connection.example" not in exported.workflow["steps"][0]["input"]["tool_revisions"]
        assert {s["name"] for s in exported.requirements["external_tools"]} == {"connection.example"}
