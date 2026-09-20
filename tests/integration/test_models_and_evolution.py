
import pytest
from fastapi import FastAPI, Request

from easyagent.contracts import EvaluationSuite, ModelRequest, Policy
from easyagent.models import HTTPProvider
from easyagent.store import Conflict
from conftest import live_server


async def test_real_http_provider_dialects_and_modalities(hub):
    app = FastAPI()
    received = []

    @app.post("/{path:path}")
    async def provider(path: str, request: Request):
        data = await request.json()
        received.append((path, data))
        if path.endswith("images/generations"):
            return {"data": [{"b64_json": "ZGVtby1maXh0dXJl"}]}
        if path.endswith("embeddings"):
            return {"data": [{"embedding": [0.1, 0.2]}]}
        if path.endswith("responses"):
            if data.get("tools") and not any(i.get("type") == "function_call_output" for i in data["input"]):
                return {"output": [{"type": "function_call", "call_id": "call-1", "name": "tool_0", "arguments": '{"text":"done"}'}]}
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"value":7}'}]}]}
        if path.endswith("messages"):
            if data.get("tools") and not any(c.get("type") == "tool_result" for m in data["messages"] for c in m["content"]):
                return {"content": [{"type": "tool_use", "id": "call-1", "name": "tool_0", "input": {"text": "done"}}]}
            return {"content": [{"type": "text", "text": '{"value":7}'}]}
        if data.get("tools") and not any(m["role"] == "tool" for m in data["messages"]):
            return {"choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "tool_0", "arguments": '{"text":"done"}'}}]}}]}
        return {"choices": [{"message": {"content": '{"value":7}'}}]}

    async with live_server(app) as url:
        for dialect in ("chat", "responses", "anthropic"):
            hub.models.register(dialect, HTTPProvider(url, "test-key", dialect), "fixture-model", {"chat", "decision", "image", "embedding"})
            created = hub.submit({"name": dialect, "steps": [{"id": "agent", "kind": "agent", "target": dialect,
                "input": {"prompt": "call the echo tool", "tools": ["core.echo"], "response_schema": {"type": "object", "required": ["value"]}}}]})
            result = await hub.wait(created)
            assert result["status"] == "succeeded", result
            assert result["steps"][0]["output"]["data"] == {"value": 7}
            assert result["steps"][0]["output"]["tool_count"] == 1
        for capability in ("image", "embedding"):
            r = await hub.models.generate(ModelRequest(model="chat", capability=capability, prompt="fixture"))
            assert r.images if capability == "image" else r.embeddings
        assert any(path == "responses" for path, _ in received)
        assert any(path == "messages" for path, _ in received)


async def test_evolution_eval_activation_snapshot_rollback(hub):
    first = hub.evolution.propose(Policy(name="organizer", model="mock", instructions="Be concise", tools=["core.echo"]))
    with pytest.raises(Conflict):
        hub.evolution.activate(first["id"])
    failed_suite = EvaluationSuite(cases=[{"prompt": "wrong", "expected": "right"}])
    assert not (await hub.evolution.evaluate(first["id"], failed_suite))["report"]["passed"]
    with pytest.raises(Conflict):
        hub.evolution.activate(first["id"])
    suite = EvaluationSuite(cases=[{"prompt": "right", "expected": "right"}])
    await hub.evolution.evaluate(first["id"], suite)
    hub.evolution.activate(first["id"])
    run = hub.submit({"name": "pinned policy", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": "hello", "policy": "organizer", "tools": ["core.echo"]}}]})
    second = hub.evolution.propose(Policy(name="organizer", model="mock", instructions="Be thorough", tools=["core.echo"]))
    await hub.evolution.evaluate(second["id"], suite)
    hub.evolution.activate(second["id"])
    assert hub.store.run(run)["spec"]["metadata"]["policy_versions"]["a"] == first["id"]
    assert hub.store.run(run)["spec"]["steps"][0]["input"]["instructions"] == "Be concise"
    hub.evolution.activate(first["id"], rollback=True)
    assert hub.evolution.active("organizer")["id"] == first["id"]
    assert (await hub.wait(run))["status"] == "succeeded"


async def test_skills_loaded_and_pinned_with_real_agent(hub, tmp_path):
    root = tmp_path / "skills"
    skill = root / "planner"
    skill.mkdir(parents=True)
    path = skill / "SKILL.md"
    path.write_text('---\nname: planner\ndescription: Organize daily tasks\n---\nKeep sources.')
    (skill / "reference.md").write_text("Evidence matters")
    hub.skills.discover(root)
    assert hub.skills.resource("planner", "reference.md") == "Evidence matters"
    with pytest.raises(ValueError):
        hub.skills.resource("planner", "../../outside")
    run = hub.submit({"name": "skill", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": "hello", "skills": ["planner"]}}]})
    path.write_text("changed after submission")
    assert "Keep sources." in hub.store.run(run)["spec"]["steps"][0]["input"]["instructions"]
    assert (await hub.wait(run))["status"] == "succeeded"
