import asyncio
import json
import time

import pytest

from easyagent.contracts import ToolSpec
from easyagent.runtime import Hub
from easyagent.store import Conflict, LeaseLost


async def test_dag_reference_condition_and_idempotency(hub):
    workflow = {"name": "dag", "steps": [
        {"id": "first", "target": "core.echo", "input": {"value": 7}},
        {"id": "second", "target": "core.echo", "depends_on": ["first"], "input": {"copied": {"$ref": "first.value"}}},
        {"id": "skip", "target": "core.echo", "depends_on": ["first"], "when": {"source": "first.value", "equals": 8}},
        {"id": "skipchild", "target": "core.echo", "depends_on": ["skip"]},
    ]}
    run_id = hub.submit(workflow, "one")
    assert hub.submit(workflow, "one") == run_id
    with pytest.raises(Conflict):
        hub.submit({**workflow, "name": "changed"}, "one")
    run = await hub.wait(run_id)
    assert run["status"] == "succeeded"
    assert run["steps"][1]["output"] == {"copied": 7}
    assert [s["status"] for s in run["steps"]][2:] == ["skipped", "skipped"]
    events = hub.store.events(run_id)
    assert len(events) >= 6
    assert hub.store.events(run_id, events[-1]["id"]) == []
    for steps in ([{"id": "a", "target": "core.echo", "depends_on": ["a"]}],
                  [{"id": "a", "target": "core.echo", "input": {"x": {"$ref": "unknown.x"}}}]):
        with pytest.raises(ValueError):
            hub.submit({"name": "bad graph", "steps": steps})


async def test_retry_timeout_delay_cancel_and_heartbeat(hub):
    count = 0

    async def flaky(args, context):
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("temporary")
        return {"attempt": count}

    async def slow(args, context):
        await asyncio.sleep(args["seconds"])
        return {"done": True}

    hub.tools.register(ToolSpec(name="test.flaky"), flaky)
    hub.tools.register(ToolSpec(name="test.slow"), slow)
    run_id = hub.submit({"name": "retry", "steps": [{"id": "a", "target": "test.flaky"}]})
    assert (await hub.wait(run_id))["steps"][0]["attempts"] == 2
    timed = hub.submit({"name": "timeout", "steps": [{"id": "a", "target": "test.slow", "input": {"seconds": 1},
                                                      "timeout_seconds": 0.05, "max_attempts": 1}]})
    assert (await hub.wait(timed))["status"] == "failed"
    future = hub.submit({"name": "later", "steps": [{"id": "a", "target": "core.echo", "not_before": time.time() + 10}]})
    await asyncio.sleep(0.03)
    assert hub.store.run(future)["steps"][0]["attempts"] == 0
    hub.store.cancel(future)
    assert (await hub.wait(future))["status"] == "cancelled"
    long = hub.submit({"name": "heartbeat", "steps": [{"id": "a", "target": "test.slow", "input": {"seconds": 0.7}}]})
    assert (await hub.wait(long))["steps"][0]["attempts"] == 1
    active = hub.submit({"name": "cancel running", "steps": [{"id": "a", "target": "test.slow", "input": {"seconds": 3}}]})
    await asyncio.sleep(0.06)
    hub.store.cancel(active)
    await asyncio.sleep(0.15)
    assert hub.store.run(active)["steps"][0]["status"] == "cancelled"


async def test_approval_restart_concurrent_resume_and_uncertain_write(tmp_path):
    db = tmp_path / "durable.db"
    calls = 0

    async def write(args, ctx):
        nonlocal calls
        calls += 1
        return {"receipt": ctx.invocation_id}

    first = Hub(db, poll_seconds=0.01, lease_seconds=0.2)
    first.tools.register(ToolSpec(name="external.write", effect="write", idempotent=False), write)
    run_id = first.submit({"name": "approval", "steps": [{"id": "a", "target": "external.write"}]})
    await first.start()
    run = await first.wait(run_id)
    assert calls == 0 and run["status"] == "waiting_approval"
    await first.stop()
    second = Hub(db, poll_seconds=0.01, lease_seconds=0.2)
    second.tools.register(ToolSpec(name="external.write", effect="write", idempotent=False), write)
    invocation_id = run["approvals"][0]["id"]
    second.tools.approve(second.store, invocation_id, True)
    with pytest.raises(Conflict):
        first.tools.approve(first.store, invocation_id, True)
    await second.start()
    assert (await second.wait(run_id))["status"] == "succeeded"
    assert calls == 1
    await second.stop()

    # Simulate the precise crash window: persisted invocation start, no receipt.
    unknown = first.submit({"name": "uncertain", "steps": [{"id": "b", "target": "external.write"}]})
    await first.start()
    pending = await first.wait(unknown)
    await first.stop()
    inv = pending["approvals"][0]["id"]
    first.tools.approve(first.store, inv, True)
    job = first.store.claim(0.1)
    with first.store.transaction() as conn:
        conn.execute("UPDATE invocations SET status='started' WHERE id=?", (inv,))
    await asyncio.sleep(0.12)
    await second.start()
    uncertain = await second.wait(unknown)
    assert uncertain["status"] == "needs_attention"
    assert calls == 1
    with pytest.raises(LeaseLost):
        first.store.finish(job, "succeeded", {"stale": True})
    second.tools.reconcile(second.store, inv, {"receipt": "verified externally"})
    complete = await second.wait(unknown)
    assert complete["status"] == "succeeded" and calls == 1
    await second.stop()


async def test_agent_checkpoints_allowlist_budget_and_approval(hub):
    async def write(args, ctx):
        return {"confirmed": args["message"]}

    hub.tools.register(ToolSpec(name="note.write", effect="write", idempotent=True), write)
    run_id = hub.submit({"name": "agent", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": json.dumps({"tool": "note.write", "arguments": {"message": "hello"}}), "tools": ["note.write"]}}]})
    pending = await hub.wait(run_id)
    assert pending["status"] == "waiting_approval"
    assert pending["steps"][0]["state"]["turns"] == 1
    hub.tools.approve(hub.store, pending["approvals"][0]["id"], True)
    run = await hub.wait(run_id)
    assert run["status"] == "succeeded"
    assert run["steps"][0]["output"]["tool_count"] == 1
    failed = hub.submit({"name": "blocked", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": '{"tool":"core.echo","arguments":{}}', "tools": ["core.echo"], "max_tool_calls": 0}}]})
    assert (await hub.wait(failed))["status"] == "failed"
    # A tool outside the allowlist is never executed; the model sees the refusal as an observation instead.
    refused = hub.submit({"name": "refused", "steps": [{"id": "a", "kind": "agent", "target": "mock", "input": {
        "prompt": '{"tool":"core.echo","arguments":{}}', "tools": ["memory.search"]}}]})
    outcome = await hub.wait(refused)
    assert outcome["status"] == "succeeded" and outcome["steps"][0]["output"]["tool_count"] == 1
    kinds = [e["kind"] for e in hub.store.events(refused)]
    assert "tool.unknown_requested" in kinds and "tool.started" not in kinds


async def test_schema_failure_prevents_side_effect_and_memory_persists(hub, tmp_path):
    called = False

    async def strict(args, ctx):
        nonlocal called
        called = True
        return {}

    hub.tools.register(ToolSpec(name="strict", input_schema={"type": "object", "required": ["value"]}), strict)
    r = hub.submit({"name": "schema", "steps": [{"id": "a", "target": "strict"},
        {"id": "b", "target": "core.echo", "depends_on": ["a"]}]})
    assert (await hub.wait(r))["status"] == "failed" and not called
    hub.store.memory_put("family", "school", {"date": "2026-10-01"}, "school notice")
    hub.store.backup(tmp_path / "backup.db")
    restored = Hub(tmp_path / "backup.db")
    assert restored.store.memory_search("family", "2026")[0]["source"] == "school notice"
