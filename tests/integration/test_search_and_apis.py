"""Actual local HTTP services + Studio + durable workflows; no paid provider calls."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from conftest import live_server
from easyagent.config import configure
from easyagent.runtime import Hub
from easyagent.search import register_tinyfish


def search_response(query, page=0):
    return {"query": query, "results": [{"position": 1, "site_name": "example.org", "title": "Synthetic source",
        "snippet": "Independent fixture, not a real search result", "url": "https://example.org/source",
        "authors": ["Fixture Author"], "year": 2026, "cited_by_count": 12}], "total_results": 1, "page": page}


async def test_tinyfish_all_three_modes_and_restart_config(api, tmp_path, monkeypatch):
    url, hub = api
    remote, calls = FastAPI(), []
    @remote.get("/")
    async def search(request: Request):
        assert request.headers["X-API-Key"] == "synthetic-key"
        calls.append(dict(request.query_params))
        return search_response(request.query_params["query"], int(request.query_params["page"]))
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        connection = {"endpoint": origin, "api_key": "synthetic-key"}
        response = await client.post("/v1/studio/search/tinyfish", json=connection)
        assert response.status_code == 201, response.text
        assert "synthetic-key" not in response.text
        assert response.json()["tool"]["effect"] == "read"
        assert "synthetic-key" not in (await client.get("/v1/tools")).text
        query = {"query": "搬家清单", "purpose": "找有来源的搬家材料", "domain_type": "research_paper",
            "pub_year_min": 2020, "pub_year_max": 2026, "page": 2, "include_domains": "arxiv.org,example.org",
            "exclude_domains": "example.net", "language": "zh", "location": "HK"}
        workflow = {"name": "search then keep citations", "steps": [
            {"id": "search", "target": "search.tinyfish", "input": query},
            {"id": "sources", "kind": "transform", "depends_on": ["search"], "input": {"sources": {"$ref": "search.results"}}}]}
        # Code and visual JSON are accepted by the same runtime; save/reload preserves mapping.
        run = await hub.wait(hub.submit(workflow))
        assert run["status"] == "succeeded", run
        assert run["steps"][1]["output"]["sources"][0]["url"] == "https://example.org/source"
        assert run["steps"][0]["output"]["results"][0]["authors"] == ["Fixture Author"]
        assert (await client.post("/v1/studio/workflows/validate", json=workflow)).status_code == 200
        saved = (await client.post("/v1/studio/workflows", json=workflow)).json()["workflow"]
        visual_id = (await client.post("/v1/runs", json=saved)).json()["id"]
        assert (await hub.wait(visual_id))["status"] == "succeeded"
        assistant = (await client.post("/v1/studio/assistants", json={"name": "Search assistant", "purpose": "Keep source URLs", "tools": ["search.tinyfish"]})).json()
        prompt = json.dumps({"tool": "search.tinyfish", "arguments": query})
        assistant_id = (await client.post(f"/v1/studio/assistants/{assistant['id']}/run", json={"message": prompt})).json()["id"]
        assert (await hub.wait(assistant_id))["status"] == "succeeded"
        assert len(calls) == 3
        assert calls[0] == {k: str(v) for k, v in query.items()}
        # Exported config uses an environment reference; works with a fresh registry/process lifecycle.
        path = tmp_path / "connections.json"
        path.write_text(json.dumps(response.json()["config"]), encoding='utf-8')
        monkeypatch.setenv("TINYFISH_API_KEY", "synthetic-key")
        restarted = Hub(tmp_path / "restart.db", poll_seconds=0.01)
        await configure(restarted, path)
        await restarted.start()
        try:
            assert (await restarted.wait(restarted.submit(workflow)))["status"] == "succeeded"
        finally:
            await restarted.stop()


async def test_tinyfish_invalid_filters_do_not_send_http(api):
    url, hub = api
    remote, calls = FastAPI(), []
    @remote.get("/")
    async def search(request: Request):
        calls.append(request.url)
        return search_response("invalid")
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        assert (await client.post("/v1/studio/search/tinyfish", json={"endpoint": origin, "api_key": "fixture"})).status_code == 201
        for invalid in [{"location": "Hong Kong"}, {"after_date": "2026-02-30"}, {"recency_minutes": 10, "before_date": "2026-01-01"},
                        {"after_date": "2026-02-01", "before_date": "2026-01-01"}, {"pub_year_min": 2026},
                        {"domain_type": "research_paper", "recency_minutes": 60},
                        {"domain_type": "research_paper", "pub_year_min": 2026, "pub_year_max": 2025}]:
            run = await hub.wait(hub.submit({"name": "reject filters", "steps": [{"id": "search", "target": "search.tinyfish", "max_attempts": 1, "input": {"query": "test", **invalid}}]}))
            assert run["status"] == "failed", invalid
        assert not calls


async def test_saved_search_keys_replace_environment_survive_restart_and_stay_private(api, tmp_path, monkeypatch):
    url, hub = api
    remote, keys = FastAPI(), []
    @remote.get('/')
    async def search(request: Request):
        keys.append(request.headers['X-API-Key'])
        return search_response(request.query_params['query'])
    monkeypatch.setenv('TINYFISH_API_KEY', 'environment-fixture')
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        register_tinyfish(hub, {'endpoint': origin})
        initial = (await client.get('/v1/studio/search/tinyfish')).json()
        assert initial['credential_source'] == 'environment' and initial['active']
        for key in ('user-a-fixture', 'user-b-fixture'):
            saved = await client.post('/v1/studio/search/tinyfish', json={
                'endpoint': origin, 'api_key': key, 'api_key_env': 'TINYFISH_API_KEY'})
            assert saved.status_code == 201, saved.text
            assert key not in saved.text and saved.json()['connection']['credential_source'] == 'saved'
            created = (await client.post('/v1/studio/search/tinyfish/test', json={'query': 'own key test'})).json()
            run = await hub.wait(created['id'])
            assert run['status'] == 'succeeded', run
            assert keys[-1] == key  # No fallback to the configured environment key.
            assert key not in json.dumps(run)
            assert key not in (await client.get('/v1/tools')).text
            assert key not in (await client.get('/v1/studio/search/tinyfish')).text
        # Blank retains the encrypted key; a rejected edit does not replace it.
        assert (await client.post('/v1/studio/search/tinyfish', json={'endpoint': origin})).status_code == 201
        rejected = await client.post('/v1/studio/search/tinyfish', json={'endpoint': 'http://untrusted.example', 'api_key': 'rejected-fixture'})
        assert rejected.status_code == 422 and 'rejected-fixture' not in rejected.text
        assert (await client.post('/v1/studio/search/tinyfish', json={'name': 'core.echo', 'api_key': 'collision-fixture'})).status_code == 422
        database = Path(hub.store.path)
        for path in database.parent.glob(database.name+'*'):
            if path.is_file():
                assert all(value.encode() not in path.read_bytes() for value in ('user-a-fixture', 'user-b-fixture'))
        await hub.stop()
        monkeypatch.delenv('TINYFISH_API_KEY')
        restored = Hub(hub.store.path, poll_seconds=.01)
        assert restored.search_connections.status()['credential_source'] == 'saved'
        # Startup/demo configuration cannot overwrite a saved user connection.
        register_tinyfish(restored, {'api_key_env': 'UNCONFIGURED_SEARCH_KEY'})
        assert restored.search_connections.status()['endpoint'] == origin
        await restored.start()
        try:
            run = await restored.wait(restored.submit({'name': 'after restart', 'steps': [
                {'id': 'search', 'target': 'search.tinyfish', 'input': {'query': 'restored'}}]}))
            assert run['status'] == 'succeeded' and keys[-1] == 'user-b-fixture'
        finally:
            await restored.stop()
        fresh = Hub(tmp_path/'independent-user.db', poll_seconds=.01)
        assert not fresh.search_connections.status()['configured']
        assert not fresh.search_connections.status()['active']
        fresh.search_connections.save({'api_key': 'user-c-fixture', 'endpoint': origin})
        await fresh.start()
        try:
            run = await fresh.wait(fresh.submit({'name': 'another user', 'steps': [
                {'id': 'search', 'target': 'search.tinyfish', 'input': {'query': 'independent'}}]}))
            assert run['status'] == 'succeeded' and keys[-1] == 'user-c-fixture'
        finally:
            await fresh.stop()


async def test_search_errors_limits_and_retry_keep_key_private(api):
    url, hub = api
    remote, counts = FastAPI(), {}
    @remote.get("/")
    async def search(request: Request):
        q = request.query_params["query"]
        counts[q] = counts.get(q, 0) + 1
        if q == "retry" and counts[q] == 1:
            return JSONResponse({"secret": request.headers["X-API-Key"]}, status_code=429)
        if q == "401":
            return JSONResponse({"secret": request.headers["X-API-Key"]}, status_code=401)
        if q == "big":
            return PlainTextResponse("x" * 1_000_001)
        if q == "json":
            return PlainTextResponse("not-json")
        if q == "shape":
            return {"results": "wrong"}
        if q == "slow":
            await asyncio.sleep(1)
        if q == "redirect":
            return RedirectResponse("/elsewhere")
        return search_response(q)
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        await client.post("/v1/studio/search/tinyfish", json={"endpoint": origin, "api_key": "secret-fixture-key", "timeout_seconds": 5})
        for q in ["retry", "401", "big", "json", "shape", "slow", "redirect"]:
            if q in ('slow', 'redirect'):
                await client.post('/v1/studio/search/tinyfish', json={
                    'endpoint': origin, 'timeout_seconds': .2 if q == 'slow' else 5})
            run = await hub.wait(hub.submit({"name": q, "steps": [{"id": "s", "target": "search.tinyfish", "max_attempts": 3 if q == "401" else 2 if q == "retry" else 1, "input": {"query": q}}]}))
            assert run["status"] == ("succeeded" if q == "retry" else "failed"), run
            assert "secret-fixture-key" not in json.dumps(run)
        assert counts["retry"] == 2 and counts["401"] == 1 and "elsewhere" not in counts


async def test_custom_http_mixed_parameters_form_text_and_post_read(api):
    url, hub = api
    remote, calls = FastAPI(), []
    @remote.post("/items/{id}/copy/{again}")
    async def mixed(id: str, again: str, request: Request):
        assert id == again == "sample"
        assert request.query_params.getlist("tags") == ["a", "b"]
        assert request.query_params["fixed"] == "1" and request.query_params["access_token"] == "synthetic"
        assert request.headers["X-Region"] == "HK" and request.cookies["locale"] == "zh"
        assert parse_qs((await request.body()).decode()) == {"query": ["hello world"]}
        calls.append(request.headers["Idempotency-Key"])
        return PlainTextResponse("synthetic result")
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        definition = {"name": "custom.search", "description": "POST search is read-only", "url": origin + "/items/{id}/copy/{id}?fixed=1",
            "method": "POST", "effect": "read", "request_encoding": "form", "response_mode": "text",
            "auth_location": "query", "auth_header": "access_token", "auth_prefix": "", "api_key": "synthetic",
            "body_parameter": "body", "parameter_locations": {"tags": "query", "X-Region": "header", "locale": "cookie"},
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "X-Region": {"type": "string"}, "locale": {"type": "string"}, "body": {"type": "object"}}, "required": ["id", "body"], "additionalProperties": False}}
        response = await client.post("/v1/studio/apis", json=definition)
        assert response.status_code == 201, response.text
        assert response.json()["config"]["http_tools"][0]["api_key_env"] == "EAH_CUSTOM_SEARCH_KEY"
        args = {"id": "sample", "tags": ["a", "b"], "X-Region": "HK", "locale": "zh", "body": {"query": "hello world"}}
        run = await hub.wait(hub.submit({"name": "mixed parameters", "steps": [{"id": "s", "target": "custom.search", "input": args}]}))
        assert run["status"] == "succeeded" and len(calls) == 1, run
        assert run["steps"][0]["output"]["text"] == "synthetic result"
        bad = definition | {"name": "bad", "auth_header": "Host" , "auth_location": "header"}
        bad["api_key"] = "do-not-echo-secret"
        rejected = await client.post("/v1/studio/apis", json=bad)
        assert rejected.status_code == 422 and "do-not-echo-secret" not in rejected.text
        rejected_search = await client.post("/v1/studio/search/tinyfish", json={"endpoint": "invalid-url", "api_key": "do-not-echo-secret"})
        assert rejected_search.status_code == 422 and "do-not-echo-secret" not in rejected_search.text


def openapi_document(origin):
    return {"openapi": "3.0.3", "info": {"title": "Synthetic inventory", "version": "1"}, "servers": [{"url": origin}],
        "security": [{"key": []}], "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        "schemas": {"Order": {"type": "object", "properties": {"product": {"type": "string"}}, "required": ["product"], "additionalProperties": False}}},
        "paths": {"/inventory/{product}": {"get": {"operationId": "lookup", "parameters": [{"name": "product", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "found", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}}}}},
        "/orders": {"post": {"operationId": "order", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}}, "responses": {"200": {"description": "receipt", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/upload": {"post": {"operationId": "upload", "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}}, "responses": {"200": {"description": "ok"}}}}}}


async def test_openapi_preview_selected_import_approval_and_atomic_failure(api):
    url, hub = api
    remote, writes = FastAPI(), []
    @remote.get("/inventory/{product}")
    async def lookup(product: str, request: Request):
        assert request.headers["X-Key"] == "fixture-token"
        return {"product": product}
    @remote.post("/orders")
    async def order(request: Request):
        assert request.headers["X-Key"] == "fixture-token"
        writes.append(await request.json())
        return {"receipt": "synthetic-order-1"}
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        body = {"document": openapi_document(origin), "prefix": "shop", "api_key": "fixture-token"}
        preview = (await client.post("/v1/studio/apis/openapi/preview", json=body)).json()
        assert [o["id"] for o in preview["operations"]] == ["lookup", "order"]
        assert preview["unsupported"][0]["operation_id"] == "upload"
        assert "fixture-token" not in json.dumps(preview)
        # Unsupported selected operation cannot leave a partially populated registry.
        failed = await client.post("/v1/studio/apis/openapi", json=body | {"operations": ["lookup", "upload"]})
        assert failed.status_code == 422 and "shop.lookup" not in hub.tools.entries
        response = await client.post("/v1/studio/apis/openapi", json=body | {"operations": ["lookup", "order"]})
        assert response.status_code == 201, response.text
        assert "fixture-token" not in response.text
        run_id = hub.submit({"name": "imported APIs", "steps": [
            {"id": "lookup", "target": "shop.lookup", "input": {"product": "lamp"}},
            {"id": "order", "target": "shop.order", "depends_on": ["lookup"], "input": {"body": {"$ref": "lookup"}}}]})
        run = await hub.wait(run_id)
        assert run["status"] == "waiting_approval" and not writes
        await client.post("/v1/approvals/" + run["approvals"][0]["id"], json={"approved": True})
        result = await hub.wait(run_id)
        assert result["status"] == "succeeded" and writes == [{"product": "lamp"}]
        assert (await client.post("/v1/studio/apis/openapi", json=body | {"operations": ["lookup"]})).status_code == 422
