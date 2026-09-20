"""Requirements -> real model HTTP -> graph -> persisted preview/export -> execution."""
import asyncio
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from conftest import live_server
from easyagent.contracts import ToolSpec
from easyagent.models import HTTPProvider


ROOT = Path(__file__).resolve().parents[2]


def report_workflow(model="planner"):
    return {"name": "Report", "metadata": {"step_labels": {"search": "搜索资料", "format": "整理来源", "summarize": "总结资料", "save": "保存报告"}}, "steps": [
        {"id": "search", "target": "research.search", "input": {"query": {"$ref": "$input.message"}}},
        {"id": "format", "target": "core.to_text", "depends_on": ["search"], "input": {"value": {"$ref": "search.results"}}},
        {"id": "summarize", "kind": "model", "target": model, "depends_on": ["format"], "input": {"messages": [
            {"role": "system", "content": "Summarize and retain URLs"}, {"role": "user", "content": {"$ref": "format.text"}}]}},
        {"id": "save", "kind": "artifact", "depends_on": ["summarize"], "input": {"name": "report.txt", "content": {"$ref": "summarize.text"}}}]}


async def register_model(hub, app, drafts, requests):
    @app.post("/chat/completions")
    async def completion(request: Request):
        payload = await request.json()
        requests.append(payload)
        if payload.get("response_format"):
            content = json.dumps(drafts[0], ensure_ascii=False)
        else:
            content = "Synthetic report with https://example.org/source"
        return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}}


async def test_no_code_build_preview_export_three_clients_and_stale_guard(api, tmp_path):
    url, hub = api
    remote, requests, effects = FastAPI(), [], []
    draft = {"workflow": report_workflow(), "explanation": "先查资料，再整理来源并总结，最后保存报告。", "questions": []}
    await register_model(hub, remote, [draft], requests)
    async def search(args, context):
        effects.append(args["query"])
        return {"results": [{"title": args["query"], "url": "https://example.org/source"}]}
    hub.tools.register(ToolSpec(name="research.search", description="Search fixture sources", input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, output_schema={"type": "object", "properties": {"results": {"type": "array"}}}), search)
    async with live_server(remote) as provider_url, httpx.AsyncClient(base_url=url) as client:
        hub.models.register("planner", HTTPProvider(provider_url), "synthetic-model", ["chat", "decision"])
        body = {"name": "我的报告助手", "purpose": "搜索材料、总结并保留来源，保存报告", "construction": "automatic", "model": "auto"}
        saved = (await client.post("/v1/studio/assistants", json=body)).json()
        path = "/v1/studio/assistants/" + saved["id"]
        assert (await client.get(path+"/export")).status_code == 409
        submitted = await client.post(path+"/build")
        assert submitted.status_code == 202, submitted.text
        build = submitted.json()["id"]
        assert (await hub.wait(build))["status"] == "succeeded"
        assert not effects, "Compilation must not call business APIs"
        view = (await client.get(path+"/workflow")).json()
        assert view["status"] == "ready", view
        workflow = view["workflow"]
        assert len(workflow["steps"]) == 4
        assert workflow["steps"][1]["input"]["value"] == {"$ref": "search.results"}
        assert (await client.get(path+"/workflow")).json()["workflow"] == workflow
        attachment = await client.get(path+"/workflow.json")
        assert attachment.json() == workflow and 'attachment' in attachment.headers['content-disposition']
        catalog_context = json.loads(requests[0]["messages"][-1]["content"])
        assert {t["name"] for t in catalog_context["available_tools"]} >= {"research.search", "core.to_text"}
        exported = await client.get(path+"/export")
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert json.loads(archive.read("workflow.json")) == workflow
            dependencies = json.loads(archive.read("dependencies.json"))
            assert {tool["name"] for tool in dependencies["tools"]} == {"research.search", "core.to_text"}
            assert dependencies["skills"] == [] and dependencies["knowledge_namespaces"] == []
            assert set(archive.namelist()) >= {"input.json", "dependencies.json", "WORKFLOW.md", "run.py", "run.mjs", "src/main.rs"}
            assert "搜索资料" in archive.read("WORKFLOW.md").decode()
            archive.extractall(tmp_path)
        (tmp_path/"input.json").write_text(json.dumps({"message": "synthetic-client-input"}), encoding='utf-8')
        env = {**os.environ, "EAH_URL": url, "CARGO_TARGET_DIR": str(ROOT/"sdk/rust/target")}
        for command in [[sys.executable, "run.py"], ["node", "run.mjs"], ["cargo", "run", "--quiet"]]:
            process = await asyncio.create_subprocess_exec(*command, cwd=tmp_path, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
            assert process.returncode == 0, stderr.decode()
            result = json.loads(stdout)
            assert result["status"] == "succeeded" and result["steps"][-1]["output"]["name"] == "report.txt", result
        assert effects == ["synthetic-client-input"]*3
        # The no-code run uses that exact plan and substitutes only this invocation's input.
        run = (await client.post(path+"/run", json={"message": "new material"})).json()["id"]
        result = await hub.wait(run)
        assert result["status"] == "succeeded" and result["spec"]["steps"] == workflow["steps"]
        artifact = await client.get('/v1/artifacts/'+result['steps'][-1]['output']['id']+'/content')
        assert artifact.status_code == 200 and "filename*=UTF-8''report.txt" in artifact.headers['content-disposition']
        assert (await client.put(path, json=body|{"purpose": "different requirement"})).status_code == 200
        assert (await client.get(path+"/workflow")).json()["status"] == "stale"
        assert (await client.post(path+"/run", json={"message": "old plan must not run"})).status_code == 409
        assert (await client.get(path+"/export")).status_code == 409


async def test_builder_missing_capability_invalid_plan_and_write_approval(api):
    url, hub = api
    remote, requests, effects = FastAPI(), [], []
    drafts = [{"workflow": None, "explanation": "没有图像输入或 OCR 服务", "questions": ["请接入图像识别能力"]}]
    await register_model(hub, remote, drafts, requests)
    async def write(args, context):
        effects.append(args)
        return {"receipt": "synthetic-only"}
    hub.tools.register(ToolSpec(name="business.write", effect="write", idempotent=False,
        input_schema={"type":"object","properties":{"value":{"type":"string"}},"required":["value"]}), write)
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        hub.models.register("planner", HTTPProvider(origin), "fixture", ["chat", "decision"])
        saved=(await client.post('/v1/studio/assistants', json={"name":"OCR","purpose":"处理图片", "construction":"automatic","model":"auto"})).json()
        path='/v1/studio/assistants/'+saved['id']
        async def build():
            submitted=await client.post(path+'/build')
            assert submitted.status_code==202,submitted.text
            identifier=submitted.json()['id']
            await hub.wait(identifier)
            return (await client.get(path+'/workflow')).json()
        missing=await build()
        assert missing['status']=='clarification' and missing['questions']
        assert (await client.get(path+'/export')).status_code==409
        drafts[0]={"workflow":{"name":"hidden work","steps":[{"id":"hidden","kind":"agent","target":"planner","input":{"prompt":"do everything", "tools":["business.write"]}}]},"explanation":"hidden","questions":[]}
        assert (await build())['status']=='invalid'
        assert not effects
        drafts[0]={"workflow":{"name":"bad literal","steps":[{"id":"write","target":"business.write","input":{"value":12}}]},"explanation":"invalid parameter","questions":[]}
        invalid=await build()
        assert invalid['status']=='invalid' and 'string' in invalid['errors'][0]
        assert (await client.get(path+'/export')).status_code==409
        drafts[0]={"workflow":{"name":"explicit write","steps":[{"id":"write","target":"business.write","input":{"value":{"$ref":"$input.message"}}}]},"explanation":"可见的写节点，需要确认","questions":[]}
        assert (await build())['status']=='ready'
        run_id=(await client.post(path+'/run',json={'message':'synthetic'})).json()['id']
        pending=await hub.wait(run_id)
        assert pending['status']=='waiting_approval' and not effects
        await client.post('/v1/approvals/'+pending['approvals'][0]['id'],json={'approved':True})
        assert (await hub.wait(run_id))['status']=='succeeded' and effects==[{'value':'synthetic'}]
        # Nested shorthand is valid Workflow input; exporting must normalize defaults recursively.
        drafts[0]={"workflow":{"name":"nested","steps":[{"id":"nested","kind":"subworkflow","body":{"name":"child","steps":[{"id":"echo","target":"core.echo","input":{"text":"child"}}]}}]},"explanation":"nested step","questions":[]}
        assert (await build())['status']=='ready'
        exported=await client.get(path+'/export')
        assert exported.status_code==200,exported.text
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert [t['name'] for t in json.loads(archive.read('dependencies.json'))['tools']]==['core.echo']


async def test_builder_no_real_model_no_empty_export_and_legacy_preserved(api):
    url, hub=api
    async with httpx.AsyncClient(base_url=url) as client:
        saved=(await client.post('/v1/studio/assistants',json={'name':'new','purpose':'write a report','construction':'automatic','model':'auto'})).json()
        path='/v1/studio/assistants/'+saved['id']
        assert (await client.post(path+'/build')).json()['status']=='waiting_connections'
        assert (await client.get(path+'/export')).status_code==409
        assert not hub.store.runs()
        legacy=(await client.post('/v1/studio/assistants',json={'name':'old','purpose':'echo'})).json()
        assert (await client.get('/v1/studio/assistants/'+legacy['id']+'/workflow')).json()['status']=='legacy'


async def test_connection_test_and_save_failure_can_retry_same_name(api):
    url, hub = api
    remote = FastAPI()
    @remote.post('/chat/completions')
    async def completion(request: Request):
        payload = await request.json()
        if payload['model'] == 'error':
            return JSONResponse({'error': 'private upstream body'}, status_code=503)
        return {'choices': [{'message': {'role': 'assistant', 'content': 'connected'}}]}
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        body = {'alias': 'new-model', 'base_url': origin, 'model': 'error', 'api_key': 'synthetic-secret'}
        failed = await client.post('/v1/studio/connections/test-and-save', json=body)
        assert failed.status_code == 502 and '503' in failed.json()['detail']
        assert 'private upstream body' not in failed.text and 'synthetic-secret' not in failed.text
        assert 'new-model' not in hub.models.bindings
        saved = await client.post('/v1/studio/connections/test-and-save', json=body | {'model': 'working'})
        assert saved.status_code == 200 and saved.json()['text'] == 'connected'
        assert hub.models.bindings['new-model'].model == 'working'
        assert (await client.post('/v1/studio/connections/new-model/test')).status_code == 200
