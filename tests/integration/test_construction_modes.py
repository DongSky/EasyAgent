"""Construction modes converge on one durable runtime. Natural-language generation is deferred."""
import json

import httpx
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.client import HubClient


async def test_preconfigured_http_api_visual_flow_save_run_and_confirmation(api):
    url, hub = api
    remote = FastAPI()
    calls, writes = [], []
    @remote.get("/inventory/{product}")
    async def inventory(product: str, request: Request):
        assert request.headers["Authorization"] == "Bearer fixture-api-key"
        calls.append(product)
        return {"product": product, "available": 3}
    @remote.post("/orders")
    async def order(request: Request):
        body = await request.json()
        writes.append((body, request.headers["Idempotency-Key"]))
        return {"receipt": "synthetic-ordered", **body}
    async with live_server(remote) as remote_url, httpx.AsyncClient(base_url=url) as client:
        lookup = {"name": "catalog.inventory", "description": "Read product availability", "url": remote_url+"/inventory/{product}",
            "api_key": "fixture-api-key", "input_schema": {"type": "object", "properties": {"product": {"type": "string"}}, "required": ["product"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"product": {"type": "string"}, "available": {"type": "integer"}}, "required": ["product", "available"]}}
        write = {"name": "catalog.order", "description": "Place a synthetic order", "url": remote_url+"/orders", "method": "POST",
                 "input_schema": {"type": "object", "properties": {"product": {"type": "string"}}, "required": ["product"], "additionalProperties": False}}
        assert (await client.post("/v1/studio/apis", json=lookup)).status_code == 201
        assert (await client.post("/v1/studio/apis", json=write)).status_code == 201
        catalog = (await client.get("/v1/tools")).text
        assert "fixture-api-key" not in catalog and remote_url not in catalog
        definition = {"name": "API nodes with wiring", "inputs": {"product": "synthetic-lamp"},
            "metadata": {"editor": {"positions": {"lookup": {"x": 30, "y": 50}, "order": {"x": 460, "y": 180}}, "viewport": {"x": 0, "y": 0, "zoom": 0.8}}},
            "limits": {"tool_calls": 2, "model_calls": 0}, "steps": [
                {"id": "lookup", "target": "catalog.inventory", "input": {"product": {"$ref": "$input.product"}}},
                {"id": "order", "target": "catalog.order", "depends_on": ["lookup"], "input": {"product": {"$ref": "lookup.product"}}}]}
        checked = await client.post("/v1/studio/workflows/validate", json=definition)
        assert checked.status_code == 200
        saved = (await client.post("/v1/studio/workflows", json=definition)).json()
        reloaded = next(w for w in (await client.get("/v1/studio/workflows")).json() if w["id"] == saved["id"])["workflow"]
        assert reloaded["metadata"] == definition["metadata"]
        assert reloaded["steps"][1]["input"] == definition["steps"][1]["input"]
        async with HubClient(url) as sdk:
            run_id = (await sdk.submit(reloaded))["id"]
            pending = await sdk.wait(run_id)
            assert pending["status"] == "waiting_approval" and calls == ["synthetic-lamp"] and not writes
            await sdk.approve(pending["approvals"][0]["id"])
            result = await sdk.wait(run_id)
            assert result["status"] == "succeeded" and len(writes) == 1
            assert result["steps"][1]["output"]["receipt"] == "synthetic-ordered"
        # Existing visual construction is available; deferred natural-language API is not.
        assert (await client.post("/v1/studio/generations", json={})).status_code == 404


async def test_visual_validation_rejects_missing_fields_cycles_and_no_code_edit_runs(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        missing = {"name": "invalid before execution", "steps": [{"id": "search", "target": "memory.search", "input": {}}]}
        assert (await client.post("/v1/studio/workflows/validate", json=missing)).status_code == 422
        cycle = {"name": "invalid wire", "steps": [{"id": "a", "target": "core.echo", "depends_on": ["b"]}, {"id": "b", "target": "core.echo", "depends_on": ["a"]}]}
        assert (await client.post("/v1/studio/workflows/validate", json=cycle)).status_code == 422
        assert not hub.store.runs()
        body = {"name": "no code", "purpose": "Echo the material", "tools": ["core.echo"], "limits": {"model_calls": 3}}
        assistant = (await client.post("/v1/studio/assistants", json=body)).json()
        identifier = assistant["id"]
        assert (await client.put("/v1/studio/assistants/"+identifier, json=body|{"name": "revised assistant"})).status_code == 200
        message = json.dumps({"tool": "core.echo", "arguments": {"text": "synthetic-input"}})
        run_id = (await client.post(f"/v1/studio/assistants/{identifier}/run", json={"message": message})).json()["id"]
        result = await hub.wait(run_id)
        assert result["status"] == "succeeded" and result["name"] == "revised assistant"
        assert result["steps"][0]["output"]["tool_count"] == 1
        assert len(hub.store.memory_search("studio-assistant-history", identifier)) == 1
