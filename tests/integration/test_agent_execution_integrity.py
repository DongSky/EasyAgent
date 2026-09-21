"""Protocol/runtime regressions. Scripted providers here do not establish NL ability."""
import asyncio
import json

import httpx
import pytest

from easyagent.contracts import ModelRequest, ModelResult, ToolCall, ToolSpec
from easyagent.model_streaming import fold_sse
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub, _pin_request


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
async def test_provider_replays_text_calls_and_opaque_continuation(dialect):
    provider = HTTPProvider("https://fixture.invalid", dialect=dialect)
    messages = [{"role": "user", "content": "inspect both"}]
    tools = [ToolSpec(name="files.read"), ToolSpec(name="files.grep")]
    wire = []

    async def post(path, payload):
        wire.append(payload)
        if dialect == "responses":
            return {"output": [
                {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-sentinel"},
                {"type": "message", "id": "m_1", "role": "assistant", "content": [{"type": "output_text", "text": "inspect both"}]},
                *[{"type": "function_call", "call_id": f"c{i}", "name": f"tool_{i}", "arguments": '{"path":"中文.py"}'} for i in range(2)]]}
        if dialect == "anthropic":
            return {"content": [{"type": "thinking", "thinking": "opaque", "signature": "signed-sentinel"},
                {"type": "text", "text": "inspect both"},
                *[{"type": "tool_use", "id": f"c{i}", "name": f"tool_{i}", "input": {"path": "中文.py"}} for i in range(2)]]}
        return {"choices": [{"message": {"content": "inspect both", "reasoning_content": "opaque-sentinel",
                "tool_calls": [{"id": f"c{i}", "function": {"name": f"tool_{i}", "arguments": '{"path":"中文.py"}'}} for i in range(2)]}}]}

    provider.post = post
    result = await provider._generate(ModelRequest(model="m", messages=messages, tools=tools), "m")
    assert [c.name for c in result.tool_calls] == ["files.read", "files.grep"]
    messages += [{"role": "assistant", "content": result.text, "tool_calls": [c.model_dump() for c in result.tool_calls],
                  "provider_state": result.provider_state}]
    messages += [{"role": "tool", "tool_call_id": f"c{i}", "content": '{"ok":true}'} for i in range(2)]
    # Historical tools still serialize after a toolkit changes, without becoming available.
    await provider._generate(ModelRequest(model="m", messages=messages, tools=tools[:1]), "m")
    encoded = json.dumps(wire[-1], ensure_ascii=False)
    assert "inspect both" in encoded
    if dialect == "chat":
        assert json.loads(wire[-1]["messages"][1]["tool_calls"][0]["function"]["arguments"])["path"] == "中文.py"
    else:
        assert "中文.py" in encoded
    assert "signed-sentinel" in encoded if dialect == "anthropic" else "opaque-sentinel" in encoded
    assert len(wire[-1]["tools"]) == 1
    assert "c0" in encoded and "c1" in encoded


@pytest.mark.parametrize("dialect", ["responses", "anthropic"])
async def test_cross_protocol_history_preserves_assistant_text(dialect):
    provider = HTTPProvider("https://fixture.invalid", dialect=dialect)
    captured = []

    async def post(path, payload):
        captured.append(payload)
        return {"output": [], "content": []}

    provider.post = post
    await provider._generate(ModelRequest(model="m", tools=[ToolSpec(name="core.echo")], messages=[
        {"role": "assistant", "content": "retain this decision", "tool_calls": [{"id": "c", "name": "core.echo", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c", "content": "{}"}]), "m")
    assert "retain this decision" in json.dumps(captured)


async def test_streaming_fragments_keep_both_calls_and_signed_thinking():
    def response(events):
        return httpx.Response(200, content="\n\n".join("data: " + (e if isinstance(e, str) else json.dumps(e)) for e in events) + "\n\n")

    events = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "tool_1", "arguments": '{"path":'}},
            {"index": 0, "id": "a", "function": {"name": "tool_0", "arguments": '{"path":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"中文.py"}'}},
            {"index": 1, "function": {"arguments": '"b.py"}'}}]}, "finish_reason": "tool_calls"}]}, "[DONE]"]
    folded = await fold_sse(response(events), "chat", 10000)
    assert [c["id"] for c in folded["choices"][0]["message"]["tool_calls"]] == ["a", "b"]
    events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "opaque"}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
              {"type": "message_stop"}]
    assert (await fold_sse(response(events), "anthropic", 10000))["content"] == [
        {"type": "thinking", "thinking": "opaque", "signature": "signed"}]
    events = [{"type": "response.output_item.added", "output_index": 0,
               "item": {"type": "function_call", "call_id": "c", "name": "tool_0", "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"x":1}'},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    assert (await fold_sse(response(events), "responses", 10000))["output"][0]["arguments"] == '{"x":1}'


async def test_malformed_call_is_observed_without_losing_valid_siblings(hub):
    rounds, executed = [], []

    async def echo(args, ctx):
        executed.append(args)
        return args

    hub.tools.register(ToolSpec(name="fixture.read"), echo)

    class Model:
        async def generate(self, request, model):
            rounds.append(request)
            if len(rounds) == 1:
                return ModelResult(tool_calls=[HTTPProvider.tool_call("bad", "fixture.read", "{broken"),
                                              ToolCall(id="good", name="fixture.read", arguments={"value": 7})])
            observed = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            assert observed[0]["error"]["code"] == "invalid_tool_arguments"
            assert observed[1] == {"value": 7}
            return ModelResult(text="verified")

    hub.models.register("batch", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "malformed batch", "steps": [{"id": "agent", "kind": "agent", "target": "batch",
                    "input": {"prompt": "act", "tools": ["fixture.read"]}}]}))
    assert run["status"] == "succeeded", run
    assert executed == [{"value": 7}]
    assert run["usage"]["model_calls"] == 2


async def test_read_batches_overlap_but_mutations_are_ordered_barriers(hub):
    started, effects, gate = [], [], asyncio.Event()

    async def read(args, ctx):
        started.append(args["n"])
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        return {"n": args["n"]}

    async def write(args, ctx):
        assert len(started) == 2
        if args["n"] == 1:
            await asyncio.sleep(.05)
        effects.append(args["n"])
        return {"n": args["n"]}

    hub.tools.register(ToolSpec(name="fixture.read"), read)
    hub.tools.register(ToolSpec(name="fixture.write", effect="local"), write)

    class Model:
        async def generate(self, request, model):
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResult(tool_calls=[ToolCall(id=str(i), name=name, arguments={"n": n}) for i, (name, n) in enumerate(
                    [("fixture.read", 1), ("fixture.read", 2), ("fixture.write", 1), ("fixture.write", 2)])])
            assert [m["tool_call_id"] for m in request.messages if m["role"] == "tool"] == ["0", "1", "2", "3"]
            return ModelResult(text="done")

    hub.models.register("batch", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "batch", "steps": [{"id": "agent", "kind": "agent", "target": "batch",
                  "input": {"prompt": "act", "tools": ["fixture.read", "fixture.write"]}}]}))
    assert run["status"] == "succeeded", run
    assert effects == [1, 2]


async def test_pending_batch_survives_restart_without_replaying_success(tmp_path):
    started, released = asyncio.Event(), asyncio.Event()
    hits = []

    async def fast(args, ctx):
        hits.append("fast")
        return {"value": 1}

    async def slow(args, ctx):
        started.set()
        await released.wait()
        return {"value": 2}

    class Model:
        async def generate(self, request, model):
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResult(tool_calls=[ToolCall(id=n, name="fixture." + n, arguments={}) for n in ("fast", "slow")])
            return ModelResult(text="done")

    def create():
        hub = Hub(tmp_path / "restart.db", concurrency=1, poll_seconds=.01, lease_seconds=10)
        hub.tools.register(ToolSpec(name="fixture.fast"), fast)
        hub.tools.register(ToolSpec(name="fixture.slow"), slow)
        hub.models.register("fixture", Model(), "fixture", ["chat"])
        return hub

    first = create()
    await first.start()
    identifier = first.submit({"name": "restart", "steps": [{"id": "agent", "kind": "agent", "target": "fixture",
                                "input": {"prompt": "act", "tools": ["fixture.fast", "fixture.slow"]}}]})
    try:
        await asyncio.wait_for(started.wait(), 10)
        async with asyncio.timeout(10):
            while "observation" not in first.store.run(identifier)["steps"][0]["state"]["pending"][0]:
                await asyncio.sleep(.01)
    finally:
        await first.stop()
    # Exercise recovery from an expired owner without a subsecond lease also
    # expiring during ordinary disk I/O on the restarted worker.
    with first.store.transaction() as db:
        assert db.execute("UPDATE steps SET lease_until=0 WHERE run_id=? AND status='running'",
                          (identifier,)).rowcount == 1
    released.set()
    second = create()
    await second.start()
    try:
        run = await second.wait(identifier)
        assert run["status"] == "succeeded", run
        assert hits == ["fast"]
        assert run["steps"][0]["attempts"] == 2
        assert run["usage"]["model_calls"] == 2 and run["usage"]["tool_calls"] == 2
    finally:
        await second.stop()


async def test_parallel_children_work_inside_parallel_workflow_nodes_with_one_worker(tmp_path):
    hub = Hub(tmp_path / "parallel.db", concurrency=1, poll_seconds=.01)
    requests = []

    class Model:
        async def generate(self, request, model):
            requests.append(request)
            goal = next(m["content"] for m in request.messages if m["role"] == "user")
            if goal.startswith("child-"):
                assert len(request.messages) == 2  # fresh, no parent transcript
                assert {t.name for t in request.tools} == {"agents.reply", "agents.note"}
                return ModelResult(text=goal)
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResult(tool_calls=[ToolCall(id="batch", name="agents.parallel", arguments={"tasks": [
                    {"goal": "child-a", "tools": []}, {"goal": "child-b", "tools": []}]})])
            results = json.loads(request.messages[-1]["content"])["results"]
            assert [r["output"]["text"] for r in results] == ["child-a", "child-b"]
            return ModelResult(text="joined")

    hub.models.register("fixture", Model(), "fixture", ["chat"])
    await hub.start()
    try:
        config = {"prompt": "parent", "tools": ["agents.parallel"],
                  "delegation": {"models": ["fixture"], "tools": [], "max_children": 2, "max_depth": 1, "max_active": 1}}
        flow = {"name": "two branches", "steps": [
            *[{"id": name, "kind": "agent", "target": "fixture", "input": config} for name in ("left", "right")],
            {"id": "join", "kind": "transform", "depends_on": ["left", "right"],
             "input": {"left": {"$ref": "left.text"}, "right": {"$ref": "right.text"}}}]}
        root = await hub.wait(hub.submit({"name": "wrapper", "steps": [{"id": "workflow", "kind": "subworkflow", "body": flow}]}))
        assert root["status"] == "succeeded", root
        run = hub.store.run(root["children"][0]["id"])
        assert run["status"] == "succeeded", run
        assert run["steps"][-1]["output"] == {"left": "joined", "right": "joined"}
        assert len(run["children"]) == 4 and len(requests) == 8
        assert run["usage"]["model_calls"] == 8
    finally:
        await hub.stop()


async def test_compaction_recomputes_pinned_positions_after_each_deletion(hub):
    await hub.stop()
    identifier = hub.submit({"name": "pins", "steps": [{"id": "a", "target": "core.echo"}]})
    job = hub.store.claim(30)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original"}]
    for i in range(4):
        messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "name": "core.echo", "arguments": {}}]},
                     {"role": "tool", "tool_call_id": str(i), "content": "x" * 1700}]
        if i == 0:
            messages.append({"role": "user", "content": "keep this later instruction"})
    state = {"messages": messages, "pinned_requests": _pin_request(messages, "")}
    await hub.compact_context(job, state, 2500)
    assert {m["content"] for m in state["messages"] if m["role"] == "user"} == {"original", "keep this later instruction"}
    assert {c["id"] for m in state["messages"] for c in m.get("tool_calls", [])} == {
        m["tool_call_id"] for m in state["messages"] if m["role"] == "tool"}
    assert hub.store.run(identifier)["status"] == "running"


async def test_repeating_identical_results_cannot_spend_tokens_forever(hub):
    class Model:
        async def generate(self, request, model):
            return ModelResult(tool_calls=[ToolCall(id="repeat", name="core.echo", arguments={"value": 1})])

    hub.models.register("loop", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "loop", "steps": [{"id": "a", "kind": "agent", "target": "loop",
                    "input": {"prompt": "act", "tools": ["core.echo"]}}]}))
    assert run["status"] == "failed"
    assert run["usage"]["model_calls"] == 4
    assert "no progress" in run["steps"][0]["error"]


async def test_large_tool_output_is_bounded_but_full_content_remains_readable(hub):
    content = "begin:" + "x" * 80000 + ":end"

    async def large(args, ctx):
        return {"document": content}

    hub.tools.register(ToolSpec(name="fixture.large"), large)

    class Model:
        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if not observations:
                return ModelResult(tool_calls=[ToolCall(id="large", name="fixture.large", arguments={})])
            value = observations[-1]
            assert value["truncated"] and len(json.dumps(value)) < 50000
            info, data = hub.artifacts.get(value["artifact"]["id"])
            assert json.loads(data)["document"] == content
            tail = hub.attachments.read(info["id"], offset=len(data.decode()) - 20, limit=20)
            assert ":end" in tail["text"] and tail["next_offset"] is None
            return ModelResult(text="done")

    hub.models.register("large", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "large result", "steps": [{"id": "a", "kind": "agent", "target": "large",
                  "input": {"prompt": "act", "tools": ["fixture.large"]}}]}))
    assert run["status"] == "succeeded", run


async def test_create_intent_cannot_finish_with_a_prose_claim(hub):
    class Model:
        async def generate(self, request, model):
            return ModelResult(text="I created and ran the workflow.")

    hub.models.register("claim", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "claim", "metadata": {"require_workflow": True}, "steps": [
        {"id": "a", "kind": "agent", "target": "claim", "input": {"prompt": "Create a workflow"}}]}))
    assert run["status"] == "failed" and run["usage"]["model_calls"] == 3
    assert "not verified" in run["steps"][0]["error"]


async def test_output_limit_recovery_is_bounded(hub):
    from easyagent.retry_policy import ModelResponseError

    class Model:
        async def generate(self, request, model):
            raise ModelResponseError("max_output_tokens")

    hub.models.register("truncated", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "truncation loop", "steps": [
        {"id": "a", "kind": "agent", "target": "truncated", "input": {"prompt": "act"}}]}))
    assert run["status"] == "failed" and run["usage"]["model_calls"] == 3


async def test_parallel_approval_is_not_hidden_behind_a_child_wait(hub):
    from easyagent.tools import WaitingChildren

    async def waiting(args, ctx):
        raise WaitingChildren()

    async def write(args, ctx):
        return {"ok": True}

    hub.tools.register(ToolSpec(name="fixture.wait", execution_mode="parallel"), waiting)
    hub.tools.register(ToolSpec(name="fixture.write", effect="write", execution_mode="parallel"), write)

    class Model:
        async def generate(self, request, model):
            return ModelResult(tool_calls=[ToolCall(id=n, name="fixture." + n, arguments={}) for n in ("wait", "write")])

    hub.models.register("pauses", Model(), "fixture", ["chat"])
    run = await hub.wait(hub.submit({"name": "pause priority", "steps": [
        {"id": "a", "kind": "agent", "target": "pauses", "input": {"prompt": "act", "tools": ["fixture.wait", "fixture.write"]}}]}))
    assert run["status"] == "waiting_approval", run
    assert len(run["approvals"]) == 1
    hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
    hub.store.cancel(run["id"])
