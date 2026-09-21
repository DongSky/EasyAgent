import io
import zipfile
import json

import httpx

from easyagent.api import create_app
from easyagent.client import HubClient
from easyagent.studio import install_studio
from easyagent.contracts import ToolSpec
from easyagent_app import mount_app
from conftest import live_server


async def test_workflow_editor_preserves_unlimited_model_wait(api):
    from playwright.async_api import async_playwright, expect
    url, hub = api
    hub.development.save_workflow('unlimited-model', {'name': '持续等待模型', 'steps': [
        {'id': 'wait', 'kind': 'model', 'target': 'mock', 'timeout_seconds': None,
         'input': {'prompt': 'hello'}}]}, 0)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(url+'/#home')
            await page.locator('[data-collection="workflows"]').click()
            await page.locator('[data-workflow="unlimited-model"]').click()
            await expect(page.locator('#nodes [name=timeout]')).to_have_value('')
            async with page.expect_response(lambda r: '/v1/studio/workflows/unlimited-model?' in r.url and r.request.method == 'PUT') as response:
                await page.locator('#saveWorkflow').click()
            saved = await (await response.value).json()
            assert saved['workflow']['steps'][0]['timeout_seconds'] is None
        finally:
            await browser.close()


async def test_http_sdk_sse_errors_and_cancel(api):
    url, hub = api
    async with HubClient(url) as client:
        created = await client.submit({"name": "python-sdk", "steps": [{"id": "a", "target": "core.echo", "input": {"hello": "Python"}}]}, "python")
        result = await client.wait(created["id"])
        assert result["steps"][0]["output"] == {"hello": "Python"}
        assert len(await client.events(created["id"])) > 2
        response = await client.http.get(f"/v1/runs/{created['id']}/stream")
        assert "event: run.created" in response.text and "succeeded" in response.text
        assert (await client.http.get("/v1/runs/missing")).status_code == 404
        assert (await client.http.post("/v1/runs", json={"name": "bad", "steps": []})).status_code == 422
        assert (await client.http.get("/v1/runs", headers={"Origin": "https://evil.example"})).status_code == 403


async def test_studio_no_code_low_code_export_and_auth(hub):
    app = create_app(hub, token="test-secret", manage_workers=False)
    install_studio(app, hub)
    mount_app(app)
    # This functional scenario includes large uploads and durable workflow writes.
    async with live_server(app) as url, httpx.AsyncClient(base_url=url, timeout=30) as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 401
        upload = '/v1/artifacts/upload?name=reference.png'
        binary = b'\x89PNG\r\n\x1a\nreference-fixture'
        assert (await client.post(upload, content=binary)).status_code == 401
        client.headers["Authorization"] = "Bearer test-secret"
        assert (await client.post(upload, content=binary, headers={'Origin': 'https://elsewhere.example'})).status_code == 403
        response = await client.post(upload, content=binary, headers={'Content-Type': 'image/png'})
        assert response.status_code == 201
        info = response.json()
        assert info['media_type'] == 'image/png' and info['size'] == len(binary)
        downloaded = await client.get('/v1/artifacts/'+info['id']+'/content')
        assert downloaded.content == binary and downloaded.headers['x-content-type-options'] == 'nosniff'
        assert (await client.post(upload, content=b'x'*50_000_001)).status_code == 413
        async def file_chunks():
            for _ in range(101):
                yield b'x'*500_000
        assert (await client.post(upload, content=file_chunks())).status_code == 413
        assert (await client.post(upload, content=b'')).status_code == 422
        assert (await client.post('/v1/runs', content=b'x'*2_000_001)).status_code == 413
        templates = (await client.get("/v1/studio/templates")).json()
        assert len(templates) == 3
        saved = (await client.post("/v1/studio/assistants", json={"name": "我的助手", "purpose": "整理事情"})).json()
        run = (await client.post(f"/v1/studio/assistants/{saved['id']}/run", json={"message": "明天搬家"})).json()
        result = await hub.wait(run["id"])
        assert result["status"] == "succeeded" and result["steps"][0]["output"]["text"] == "明天搬家"
        export = await client.get(f"/v1/studio/assistants/{saved['id']}/export")
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            assert {"run.py", "run.mjs", "src/main.rs", "workflow.json"}.issubset(archive.namelist())
            compile(archive.read("run.py"), "run.py", "exec")
        workflow = {"name": "低代码流程", "steps": [{"id": "a", "target": "core.echo", "input": {"ok": True}},
            {"id": "b", "target": "core.echo", "depends_on": ["a"], "when": {"source": "a.ok", "equals": True},
             "input": {"received": {"$ref": "a.ok"}}}]}
        saved_workflow = await client.post("/v1/studio/workflows", json=workflow)
        assert saved_workflow.status_code == 201
        assert len((await client.get("/v1/studio/workflows")).json()) == 1
        run = (await client.post("/v1/runs", json=workflow)).json()
        assert (await hub.wait(run["id"]))["steps"][1]["output"] == {"received": True}


async def test_studio_validation_budget_export_and_chunked_request_limit(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        body = {"name": "bounded assistant", "purpose": "echo", "limits": {"model_calls": 1}}
        saved = (await client.post("/v1/studio/assistants", json=body)).json()
        path = f"/v1/studio/assistants/{saved['id']}"
        assert (await client.put(path, json=body | {"tools": ["unavailable"]})).status_code == 422
        assert (await client.put(path, json=body | {"strategy": "unknown"})).status_code == 422
        assert (await client.put(path, json=body | {"capability": "video"})).status_code == 422
        exported = await client.get(path + "/export")
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert json.loads(archive.read("workflow.json"))["limits"]["model_calls"] == 1
        run = (await client.post(path + "/run", json={"message": "hello"})).json()
        assert (await hub.wait(run["id"]))["usage"]["model_calls"] == 1
        definition = {"name": "preserve config", "inputs": {"x": "hello"}, "limits": {"tool_calls": 0},
                      "metadata": {"source": "synthetic"}, "steps": [{"id": "value", "kind": "transform", "input": {"value": {"$ref": "$input.x"}}}]}
        workflow = (await client.post("/v1/studio/workflows", json=definition)).json()["workflow"]
        assert workflow["inputs"] == definition["inputs"] and workflow["limits"]["tool_calls"] == 0
        assert (await hub.wait(hub.submit(workflow)))["steps"][0]["output"] == {"value": "hello"}
        async def chunks():
            for _ in range(5):
                yield b"x" * 500_000
        assert (await client.post("/v1/runs", content=chunks())).status_code == 413


async def test_reconcile_uncertain_effect_requires_atomic_receipt(api):
    url, hub = api
    effects = []
    async def uncertain(args, context):
        effects.append(context.invocation_id)
        raise ConnectionError("simulated lost receipt after external acceptance")
    hub.tools.register(ToolSpec(name="receipt.write", effect="write", idempotent=False), uncertain)
    identifier = hub.submit({"name": "reconcile via API", "steps": [{"id": "write", "target": "receipt.write"}]})
    pending = await hub.wait(identifier)
    invocation = pending["approvals"][0]["id"]
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        await client.post("/v1/approvals/"+invocation, json={"approved": True})
        assert (await hub.wait(identifier))["status"] == "needs_attention"
        path = "/v1/reconciliations/"+invocation
        assert (await client.post(path, json={"output": {"done": True}, "receipt": "  "})).status_code == 422
        assert hub.store.run(identifier)["status"] == "needs_attention"
        assert (await client.post(path, json={"output": {"done": True}, "receipt": "synthetic verified receipt"})).status_code == 200
        assert (await hub.wait(identifier))["status"] == "succeeded"
        assert len(effects) == 1
        records = hub.store.memory_search("receipts", invocation)
        assert records[0]["value"] == "synthetic verified receipt"
        assert (await client.post(path, json={"output": {}, "receipt": "duplicate"})).status_code == 409


async def test_memory_selection_merge_history_and_stale_edit_over_http(api):
    """The UI selects server digests; a concurrent edit must not be silently merged."""
    from easyagent.components import digest

    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        for key, value in {"time": "下午", "place": {"city": "香港", "floor": 2}}.items():
            response = await client.put(f"/v1/memory/ui-test/{key}", json={"value": value, "source": "用户确认"})
            assert response.status_code == 200
        ordinary = (await client.get("/v1/memory/ui-test")).json()
        assert all("digest" not in row for row in ordinary)
        selected = (await client.get("/v1/memory/ui-test?include_digest=true")).json()
        assert all(row["digest"] == digest(row["value"]) for row in selected)
        body = {"destination": "preferences", "sources": {r["key"]: r["digest"] for r in selected}, "value": "下午在香港"}
        await client.put("/v1/memory/ui-test/time", json={"value": "上午", "source": "新确认"})
        assert (await client.post("/v1/memory/ui-test/merge", json=body)).status_code == 409
        assert len((await client.get("/v1/memory/ui-test")).json()) == 2
        assert (await client.get("/v1/memory/ui-test/history")).json() == []
        selected = (await client.get("/v1/memory/ui-test?include_digest=true")).json()
        body["sources"] = {r["key"]: r["digest"] for r in selected}
        body["value"] = "上午在香港"
        assert (await client.post("/v1/memory/ui-test/merge", json=body)).status_code == 200
        history = (await client.get("/v1/memory/ui-test/history")).json()
        assert len(history) == 2 and {r["source"] for r in history} == {"用户确认", "新确认"}
        current = (await client.get("/v1/memory/ui-test")).json()
        assert current[0]["key"] == "preferences" and current[0]["value"] == "上午在香港"


async def test_task_search_and_status_filter_apply_before_recent_limit(api):
    url, hub = api
    older = hub.submit({"name": "Older UI acceptance", "steps": [{"id": "echo", "target": "core.echo"}]})
    await hub.wait(older)
    newer = hub.submit({"name": "Newer unrelated", "steps": [{"id": "echo", "target": "core.echo"}]})
    await hub.wait(newer)
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        recent = (await client.get("/v1/runs?limit=1")).json()
        assert recent[0]["id"] == newer
        found = (await client.get("/v1/runs", params={"limit": 1, "query": "ui acceptance", "status": "succeeded"})).json()
        assert [r["id"] for r in found] == [older]
        assert (await client.get("/v1/runs", params={"query": "ui acceptance", "status": "failed"})).json() == []
        assert (await client.get("/v1/runs", params={"query": "%' OR 1=1 --"})).json() == []
