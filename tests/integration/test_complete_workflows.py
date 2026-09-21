import asyncio
import json
import time

import httpx

from easyagent.contracts import EvaluationSuite, ModelResult, Policy, ToolSpec
from easyagent.scheduling import Trigger


async def test_foreach_subworkflow_approval_and_parent_budget(hub):
    body = {"name": "child", "steps": [{"id": "echo", "kind": "transform", "input": {"item": {"$ref": "$input.item"}}},
        {"id": "approve", "kind": "approval", "depends_on": ["echo"], "input": {"item": {"$ref": "echo.item"}}}]}
    identifier = hub.submit({"name": "batch", "steps": [{"id": "batch", "kind": "foreach", "input": {"items": [1, 2, 3]}, "body": body}]})
    pending = await hub.wait(identifier)
    async with asyncio.timeout(30):
        # A parent can be checking its children while the last approval appears.
        # Wait for both the complete approval set and the parent's waiting state.
        while len(pending["approvals"]) < 3 or pending["status"] != "waiting_approval":
            await asyncio.sleep(0.01)
            pending = hub.store.run(identifier)
    assert pending["status"] == "waiting_approval" and len(pending["approvals"]) == 3
    for approval in pending["approvals"]:
        hub.tools.approve(hub.store, approval["id"], True)
    completed = await hub.wait(identifier)
    assert completed["status"] == "succeeded", completed
    assert [r["echo"]["item"] for r in completed["steps"][0]["output"]["results"]] == [1, 2, 3]
    assert completed["usage"]["tool_calls"] == 3
    limited = hub.submit({"name": "limited tree", "limits": {"model_calls": 1}, "steps": [{"id": "loop", "kind": "foreach", "input": {"items": [1, 2]},
        "body": {"name": "model child", "steps": [{"id": "model", "kind": "model", "target": "mock", "input": {"prompt": "hello"}}]}}]})
    assert (await hub.wait(limited))["status"] == "failed"
    assert hub.store.run(limited)["usage"]["model_calls"] == 1


async def test_foreach_preserves_explicit_shared_inputs_and_defaults_across_restart(hub):
    from easyagent.runtime import Hub
    body = {'name': '单张图片', 'inputs': {'quality': 'medium', 'edit_prompt': 'default'}, 'steps': [
        {'id': 'edit', 'target': 'core.echo', 'requires_approval': True, 'input': {
            'image': {'$ref': '$input.item'}, 'prompt': {'$ref': '$input.edit_prompt'},
            'index': {'$ref': '$input.index'}, 'quality': {'$ref': '$input.quality'}}}]}
    identifier = hub.submit({'name': '批量修图参数', 'inputs': {'private': 'not inherited'}, 'steps': [
        {'id': 'prepare_prompt', 'kind': 'transform', 'input': {'text': '自然重打光、保留纹理'}},
        {'id': 'retouch', 'kind': 'foreach', 'depends_on': ['prepare_prompt'], 'input': {
            'items': ['original-one', 'original-two'], 'edit_prompt': {'$ref': 'prepare_prompt.text'},
            'item': 'must not override the image', 'index': 99}, 'body': body}]})
    async with asyncio.timeout(30):
        while len(hub.store.run(identifier)['approvals']) < 2:
            await asyncio.sleep(.01)
    await hub.stop()
    restored = Hub(hub.store.path, poll_seconds=.01)
    for approval in restored.store.run(identifier)['approvals']:
        restored.tools.approve(restored.store, approval['id'], True)
    await restored.start()
    try:
        run = await restored.wait(identifier)
        assert run['status'] == 'succeeded', run
        results = run['steps'][1]['output']['results']
        assert [r['edit'] for r in results] == [
            {'image': image, 'prompt': '自然重打光、保留纹理', 'index': index, 'quality': 'medium'}
            for index, image in enumerate(['original-one', 'original-two'])]
        assert len(run['children']) == 2 and run['usage']['tool_calls'] == 2
        for child in run['children']:
            assert 'private' not in restored.store.run(child['id'])['spec']['inputs']
    finally:
        await restored.stop()


async def test_knowledge_to_agent_artifact_and_trace(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        document = {"namespace": "home", "title": "搬家约定", "source": "notice-1", "text": "搬家日期是 2026-10-01。电梯需要提前预约。", "embedding_model": "mock"}
        ingest = await client.post("/v1/knowledge/documents", json=document)
        assert ingest.status_code == 201
        first = ingest.json()
        assert (await client.post("/v1/knowledge/documents", json=document)).json()["id"] == first["id"]
        for mode in ("lexical", "vector", "hybrid"):
            found = (await client.post("/v1/knowledge/search", json={"namespace": "home", "query": "搬家日期", "mode": mode})).json()
            assert found["citations"][0]["source"] == "notice-1"
        run = hub.submit({"name": "knowledge pipeline", "steps": [
            {"id": "retrieve", "kind": "retrieve", "input": {"namespace": "home", "query": "搬家"}},
            {"id": "save", "kind": "artifact", "depends_on": ["retrieve"], "input": {"name": "citations.json", "content": {"$ref": "retrieve"}, "media_type": "application/json"}}]})
        completed = await hub.wait(run)
        assert completed["status"] == "succeeded"
        artifact_id = completed["steps"][1]["output"]["id"]
        response = await client.get(f"/v1/artifacts/{artifact_id}/content")
        assert response.json()["citations"][0]["source"] == "notice-1"
        assert "attachment" in response.headers["content-disposition"]
        assert (await client.get(f"/v1/runs/{run}/trace")).json()["artifacts"][0]["id"] == artifact_id
        hub.store.memory_put("family", "expired", "private", expires=time.time()-1)
        assert hub.store.memory_search("family") == []


async def test_plan_execute_observe_replan_and_output_guard(hub):
    class Planner:
        async def generate(self, request, model):
            observed = any("Observed tool result" in m["content"] for m in request.messages)
            data = {"done": observed, "answer": "verified completion" if observed else "", "plan": [] if observed else [
                {"tool": "core.echo", "arguments": {"evidence": "receipt"}, "purpose": "check evidence"}]}
            return ModelResult(text=json.dumps(data), data=data, usage={"output_tokens": 20})
    hub.models.register("planner", Planner(), "protocol-fixture", {"decision"})
    run = hub.submit({"name": "planning", "steps": [{"id": "agent", "kind": "agent", "target": "planner", "input": {
        "prompt": "finish with evidence", "strategy": "plan_execute", "tools": ["core.echo"]}}]})
    result = await hub.wait(run)
    assert result["status"] == "succeeded", result
    assert result["steps"][0]["output"]["turns"] == 2
    assert result["usage"]["model_calls"] == 2 and result["usage"]["tokens"] == 40
    guarded = hub.submit({"name": "output guard", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": "secret-value", "forbidden_output": ["secret-value"]}}]})
    assert (await hub.wait(guarded))["status"] == "failed"


async def test_model_fallback_and_cost_budget(hub):
    class Down:
        async def generate(self, request, model):
            raise RuntimeError("provider unavailable")
    hub.models.register("down", Down(), "down", {"chat"}, fallback="mock")
    run = hub.submit({"name": "fallback", "steps": [{"id": "a", "kind": "model", "target": "down", "input": {"prompt": "hello"}}]})
    completed = await hub.wait(run)
    assert completed["status"] == "succeeded"
    assert completed["usage"]["model_calls"] == 2
    assert completed["steps"][0]["output"]["usage"]["model_alias"] == "mock"
    limited = hub.submit({"name": "unknown cost blocked", "limits": {"cost_usd": 0.01}, "steps": [
        {"id": "a", "kind": "model", "target": "down", "input": {"prompt": "hello"}}]})
    result = await hub.wait(limited)
    assert result["status"] == "failed" and result["usage"]["model_calls"] == 0


async def test_triggers_dedup_pause_and_missed_schedule(api):
    url, hub = api
    workflow = {"name": "triggered", "steps": [{"id": "a", "kind": "transform", "input": {"payload": {"$ref": "$input"}}}]}
    hook = hub.scheduler.create(Trigger(name="inbox", kind="webhook", workflow=workflow))
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        path = f"/v1/triggers/{hook['id']}/fire"
        a = (await client.post(path, json={"notice": "hello"}, headers={"Idempotency-Key": "message-1"})).json()
        b = (await client.post(path, json={"notice": "hello"}, headers={"Idempotency-Key": "message-1"})).json()
        assert a == b
        assert (await hub.wait(a["id"]))["steps"][0]["output"]["payload"] == {"notice": "hello"}
        hub.scheduler.enable(hook["id"], False)
        assert (await client.post(path, json={}, headers={"Idempotency-Key": "2"})).status_code == 404
    scheduled = hub.scheduler.create(Trigger(name="daily", kind="schedule", workflow=workflow, interval_seconds=60, start_at=time.time()-3600))
    hub.scheduler.tick()
    hub.scheduler.tick()
    rows = [r for r in hub.store.runs() if r["name"] == "triggered"]
    assert len(rows) == 2
    trigger = next(t for t in hub.scheduler.list() if t["id"] == scheduled["id"])
    assert trigger["next_at"] > time.time()


async def test_failure_compensation_respects_approval(hub):
    compensations = []
    async def fail(args, context):
        raise ValueError("cannot finish")
    async def undo(args, context):
        compensations.append(context.invocation_id)
        return {"undone": True}
    hub.tools.register(ToolSpec(name="test.fail"), fail)
    hub.tools.register(ToolSpec(name="test.undo", effect="write"), undo)
    run = hub.submit({"name": "compensate", "steps": [{"id": "a", "target": "test.fail", "compensate": {"target": "test.undo", "input": {}}}]})
    pending = await hub.wait(run)
    assert pending["status"] == "waiting_approval" and not compensations
    hub.tools.approve(hub.store, pending["approvals"][0]["id"], True)
    result = await hub.wait(run)
    assert result["status"] == "failed" and len(compensations) == 1
    assert "compensation completed" in result["steps"][0]["error"]


async def test_eval_runs_agent_tools_and_feedback_proposal(hub):
    policy = hub.evolution.propose(Policy(name="echo", model="mock", instructions="Use evidence", tools=["core.echo"]))
    suite = EvaluationSuite(cases=[{"prompt": '{"tool":"core.echo","arguments":{"text":"verified"}}',
                                    "expected": "verified", "forbidden": ["invented"], "expected_tools": ["core.echo"]}])
    report = (await hub.evolution.evaluate(policy["id"], suite))["report"]
    assert report["passed"] and report["cases"][0]["tools"] == ["core.echo"]
    hub.evolution.activate(policy["id"])
    class Proposer:
        async def generate(self, request, model):
            return ModelResult(data={"instructions": "Use evidence and preserve dates"})
    hub.models.register("proposer", Proposer(), "fixture", {"decision"})
    proposal = await hub.evolution.improve("echo", "Preserve dates", "proposer")
    assert proposal["candidate"]["status"] == "candidate"
    assert proposal["candidate"]["body"]["tools"] == ["core.echo"]
    assert hub.evolution.active("echo")["id"] == policy["id"]
