"""Benchmark-family adaptations; assertions measure runtime behavior, not model quality."""
import json

import httpx
import pytest

from conftest import live_server
from easyagent.api import create_app
from easyagent.contracts import ModelResult, ToolCall, ToolSpec
from easyagent.runtime import Hub
from examples.scenarios.world import ScenarioWorld, workflow


@pytest.fixture
def world(hub, tmp_path):
    world = ScenarioWorld(tmp_path / "business.db")
    world.register(hub)
    return world


async def test_known_time_reminders_with_source_and_artifact(hub, world):
    result = await hub.wait(hub.submit(workflow("reminder_known")))
    assert result["status"] == "succeeded", result
    assert world.lookups == [{"after": "2026-09-19T09:00:00+08:00"}]
    assert result["steps"][1]["output"]["items"][0]["id"] == "next"
    artifact = hub.artifacts.list(result["id"])[0]
    assert artifact["name"] == "reminders.json"
    assert result["steps"][1]["output"]["source"] == "fixture-reminders"


async def test_missing_time_nested_pause_restart_and_single_resume(tmp_path):
    path = tmp_path / "hub.db"
    hub = Hub(path, poll_seconds=0.01)
    world = ScenarioWorld(tmp_path / "business.db")
    world.register(hub)
    await hub.start()
    identifier = hub.submit({"name": "parent waiting for user", "steps": [
        {"id": "child", "kind": "subworkflow", "body": workflow("reminder_missing")} ]})
    try:
        pending = await hub.wait(identifier)
        assert pending["status"] == "waiting_input" and not world.lookups
    finally:
        await hub.stop()
    hub = Hub(path, poll_seconds=0.01)
    world.register(hub)
    async with live_server(create_app(hub)) as url, httpx.AsyncClient(base_url=url, timeout=30) as client:
        pending = (await client.get("/v1/runs/" + identifier)).json()
        request_id = pending["input_requests"][0]["id"]
        for invalid in ({}, {"after": "unknown"}, {"after": "2026-09-19T09:00:00"}):
            assert (await client.post("/v1/inputs/" + request_id, json=invalid)).status_code == 422
            assert not world.lookups
        body = {"after": "2026-09-19T09:00:00+08:00"}
        assert (await client.post("/v1/inputs/" + request_id, json=body)).status_code == 200
        assert (await client.post("/v1/inputs/" + request_id, json=body)).status_code == 409
        result = await hub.wait(identifier)
        assert result["status"] == "succeeded", result
        assert world.lookups == [body]
        cancelled = hub.submit(workflow("reminder_missing"))
        question = (await hub.wait(cancelled))["input_requests"][0]["id"]
        hub.store.cancel(cancelled)
        assert (await client.post("/v1/inputs/" + question, json=body)).status_code == 409


@pytest.mark.parametrize("action", ["approve", "deny", "already_shipped", "ships_while_waiting", "wrong_identity"])
async def test_order_policy_confirmation_and_actual_effect(api, world, action):
    url, hub = api
    definition = workflow("order_cancel")
    if action == "already_shipped":
        with world.connect() as db:
            db.execute("UPDATE orders SET status='shipped'")
    if action == "wrong_identity":
        definition["inputs"]["credential"] = "invalid"
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        a = await client.post("/v1/runs", json=definition, headers={"Idempotency-Key": "cancel-message"})
        identifier = a.json()["id"]
        assert (await client.post("/v1/runs", json=definition, headers={"Idempotency-Key": "cancel-message"})).json()["id"] == identifier
        pending = await hub.wait(identifier)
        with world.connect() as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        if action in ("approve", "deny", "ships_while_waiting"):
            assert pending["status"] == "waiting_approval"
            approval = pending["approvals"][0]
            assert approval["arguments"] == {"customer": "customer-demo", "id": "order-demo", "reason": "no_longer_needed"}
            if action == "ships_while_waiting":
                with world.connect() as db:
                    db.execute("UPDATE orders SET status='shipped'")
            assert (await client.post("/v1/approvals/" + approval["id"], json={"approved": action != "deny"})).status_code == 200
            result = await hub.wait(identifier)
        else:
            result = pending
        if action == "approve":
            assert result["status"] == "succeeded", result
            artifact_id = result["steps"][-1]["output"]["id"]
            assert (await client.get(f"/v1/artifacts/{artifact_id}/content")).json()["order_id"] == "order-demo"
        elif action == "already_shipped":
            assert result["steps"][2]["status"] == "skipped"
        else:
            assert result["status"] == "failed", result
        with world.connect() as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == int(action == "approve")
            state = db.execute("SELECT status FROM orders").fetchone()[0]
            assert (state == "cancelled") == (action == "approve")


async def test_parallel_tools_join_and_missing_parameter(api, world):
    url, hub = api
    result = await hub.wait(hub.submit(workflow("parallel_quotes")))
    assert result["status"] == "succeeded", result
    assert result["steps"][-1]["output"] == {"cents": 20000, "currency": "CNY"}
    assert world.peak_parallel == 2
    invalid = hub.submit({"name": "missing argument", "steps": [{"id": "query", "target": "scenario.reminders"}]})
    assert (await hub.wait(invalid))["status"] == "failed"
    assert not world.lookups
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        response = await client.post("/v1/runs", json={"name": "missing function", "steps": [{"id": "x", "target": "scenario.unavailable"}]})
        assert response.status_code == 422


async def test_untrusted_content_cannot_expand_agent_tool_permissions(hub, world):
    class AdversarialFixture:
        async def generate(self, request, model):
            observed = any(m["role"] == "tool" for m in request.messages)
            if observed:
                assert "SYSTEM:" in request.messages[-1]["content"]
            name = "scenario.exfiltrate" if observed else "scenario.reviews"
            return ModelResult(tool_calls=[ToolCall(id="attack" if observed else "read", name=name, arguments={})])
    hub.models.register("adversarial-fixture", AdversarialFixture(), "deliberately-noncompliant", {"chat"})
    result = await hub.wait(hub.submit(workflow("untrusted_reviews")))
    # The injected tool is refused as an observation; the noncompliant fixture keeps insisting until its budget ends.
    assert result["status"] == "failed" and "budget" in result["steps"][0]["error"]
    assert "tool.unknown_requested" in {e["kind"] for e in hub.store.events(result["id"])}
    with hub.store.connect() as db:
        assert db.execute("SELECT count(*) FROM invocations WHERE tool='scenario.exfiltrate'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM invocations WHERE tool='scenario.reviews' AND status='succeeded'").fetchone()[0] == 1


async def test_long_context_retains_request_memory_source_and_complete_tool_pairs(hub):
    hub.store.memory_put("travel", "preference", "early arrival", source="confirmed-by-user")
    async def lengthy(args, ctx):
        return {"number": args["number"], "text": "x" * 1100, "source": "synthetic-record"}
    hub.tools.register(ToolSpec(name="scenario.long"), lengthy)
    class LongContextFixture:
        async def generate(self, request, model):
            assert "Keep original request" in json.dumps(request.messages)
            assert "confirmed-by-user" in json.dumps(request.messages)
            calls = {c["id"] for m in request.messages for c in m.get("tool_calls", [])}
            results = {m["tool_call_id"] for m in request.messages if m["role"] == "tool"}
            assert calls == results
            observed = [json.loads(m["content"])["number"] for m in request.messages if m["role"] == "tool"]
            number = max(observed, default=0) + 1
            if number > 5:
                return ModelResult(text="source: confirmed-by-user; completed")
            return ModelResult(tool_calls=[ToolCall(id=str(number), name="scenario.long", arguments={"number": number})])
    hub.models.register("long-fixture", LongContextFixture(), "fixture", {"chat"})
    identifier = hub.submit({"name": "long context", "steps": [{"id": "a", "kind": "agent", "target": "long-fixture", "input": {
        "prompt": "Keep original request", "tools": ["scenario.long"], "memory_namespaces": ["travel"], "context_chars": 4000}}]})
    result = await hub.wait(identifier)
    assert result["status"] == "succeeded", result
    assert result["steps"][0]["output"]["tool_count"] == 5
    assert any(e["kind"] == "context.compacted" for e in hub.store.events(identifier))
