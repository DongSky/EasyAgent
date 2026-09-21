import asyncio
import json

import pytest

from easyagent.contracts import EvaluationSuite, ModelResult, ToolCall
from easyagent.store import Conflict


class LearningModel:
    async def generate(self, request, model):
        if request.response_schema:
            return ModelResult(
                data={
                    "instructions": "Compare the quantity and currency before computing a quote; verify the total and disclose missing fees."
                },
                usage={"mock": True},
            )
        return ModelResult(text="quantity currency verified", usage={"mock": True})


async def test_learning_run_review_eval_publish_reuse_rollback(hub):
    hub.models.register("teacher", LearningModel(), "fixture", ["chat", "decision"])
    source = await hub.wait(
        hub.submit(
            {"name": "evidence", "steps": [{"id": "x", "target": "core.echo", "input": {"receipt": "valid"}}]}
        )
    )
    candidate = await hub.learning.propose({"run_id": source["id"], "name": "quotes", "model": "teacher"})
    identifier = candidate["candidate"]["id"]
    with pytest.raises(Conflict):
        hub.learning.publish(identifier)
    failed = await hub.evolution.evaluate(
        identifier, EvaluationSuite(cases=[{"prompt": "quote", "expected": "missing"}])
    )
    assert not failed["report"]["passed"]
    good = await hub.evolution.evaluate(
        identifier, EvaluationSuite(cases=[{"prompt": "quote", "expected": "verified"}])
    )
    assert good["report"]["passed"]
    hub.learning.publish(identifier)
    for name in ["moving quote", "repair quote"]:
        flow = {
            "name": name,
            "steps": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "target": "teacher",
                    "input": {"prompt": "quote", "skills": ["learned.quotes"]},
                }
            ],
        }
        run = await hub.wait(hub.submit(flow))
        assert run["status"] == "succeeded"
        assert "Compare the quantity" in run["spec"]["steps"][0]["input"]["instructions"]
    next_candidate = await hub.learning.propose(
        {"run_id": source["id"], "name": "quotes", "model": "teacher"}
    )
    next_id = next_candidate["candidate"]["id"]
    await hub.evolution.evaluate(
        next_id, EvaluationSuite(cases=[{"prompt": "quote", "expected": "verified"}])
    )
    hub.learning.publish(next_id)
    assert hub.learning.publish(identifier, True)["status"] == "active"


class DelegatingModel:
    async def generate(self, request, model):
        if model == "child":
            return ModelResult(text="child finished", usage={"mock": True})
        observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
        if observations and isinstance(observations[-1].get("error"), dict):
            return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
        if not observations:
            return ModelResult(
                tool_calls=[
                    ToolCall(
                        id="spawn",
                        name="agents.spawn",
                        arguments={"goal": "do a subtask", "model": "child", "tools": []},
                    )
                ],
                usage={"mock": True},
            )
        if len(observations) == 1:
            return ModelResult(
                tool_calls=[ToolCall(id="wait", name="agents.wait", arguments={"id": observations[0]["id"]})],
                usage={"mock": True},
            )
        return ModelResult(text=observations[-1]["output"]["text"], usage={"mock": True})


async def test_dynamic_delegation_single_worker_and_capability_denial(hub):
    await hub.stop()
    hub.concurrency = 1
    hub.models.register("parent", DelegatingModel(), "parent", ["chat"])
    hub.models.register("child", DelegatingModel(), "child", ["chat"])
    await hub.start()
    flow = {
        "name": "delegate",
        "steps": [
            {
                "id": "agent",
                "kind": "agent",
                "target": "parent",
                "input": {
                    "prompt": "delegate",
                    "tools": ["agents.spawn", "agents.wait"],
                    "delegation": {"models": ["child"], "tools": []},
                },
            }
        ],
    }
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["output"]["text"] == "child finished"
    assert run["usage"]["child_runs"] == 1
    flow["steps"][0]["input"]["delegation"]["models"] = []
    denied = await hub.wait(hub.submit(flow))
    # The parent learns about the refusal instead of crashing; no child is created.
    assert denied["status"] == "succeeded" and "grant" in denied["steps"][0]["output"]["text"]
    assert denied["usage"]["child_runs"] == 0 and not denied["children"]


async def test_subagent_result_is_bounded_and_carries_a_hash(hub):
    """The parent reads a child's answer, never its transcript, and never an unbounded one."""
    class LongChild:
        async def generate(self, request, model):
            if model == "child":
                return ModelResult(text="y" * 40000, usage={"mock": True})
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused", usage={"mock": True})
            if not observations:
                return ModelResult(
                    tool_calls=[ToolCall(id="spawn", name="agents.spawn",
                                         arguments={"goal": "answer at length", "model": "child", "tools": []})],
                    usage={"mock": True},
                )
            if len(observations) == 1:
                return ModelResult(
                    tool_calls=[ToolCall(id="wait", name="agents.wait", arguments={"id": observations[0]["id"]})],
                    usage={"mock": True},
                )
            result = observations[-1]
            text = result["output"]["text"]
            return ModelResult(text=json.dumps({
                "length": len(text), "truncated": "[truncated" in text,
                "hashed": bool(result.get("result_hash")),
                "status": result["status"],
            }), usage={"mock": True})

    hub.models.register("parent", LongChild(), "parent", ["chat"])
    hub.models.register("child", LongChild(), "child", ["chat"])
    flow = {
        "name": "long delegate",
        "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
            "prompt": "delegate", "tools": ["agents.spawn", "agents.wait"],
            "delegation": {"models": ["child"], "tools": []}}}],
    }
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    payload = json.loads(run["steps"][0]["output"]["text"])
    assert payload["status"] == "succeeded"
    assert payload["truncated"] and payload["hashed"]
    assert payload["length"] < 40000


async def test_subagent_result_schema_corrects_once_then_keeps_unverified_work(hub):
    """A result contract gets one bounded correction round; the work is never thrown away."""
    schema = {"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"],
              "additionalProperties": False}

    class Child:
        def __init__(self, obey):
            self.obey, self.attempts = obey, 0

        async def generate(self, request, model):
            if model == "child":
                self.attempts += 1
                if self.obey and self.attempts > 1:
                    return ModelResult(text='{"total": 5}', usage={"mock": True})
                return ModelResult(text="Alice totals five, Bob four; see the table above.", usage={"mock": True})
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused", usage={"mock": True})
            if not observations:
                return ModelResult(
                    tool_calls=[ToolCall(id="spawn", name="agents.spawn",
                                         arguments={"goal": "count", "model": "child", "tools": [],
                                                    "response_schema": schema})],
                    usage={"mock": True})
            if len(observations) == 1:
                return ModelResult(
                    tool_calls=[ToolCall(id="wait", name="agents.wait", arguments={"id": observations[0]["id"]})],
                    usage={"mock": True})
            return ModelResult(text=json.dumps(observations[-1]), usage={"mock": True})

    flow = {"name": "schema delegate", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "count", "tools": ["agents.spawn", "agents.wait"],
        "delegation": {"models": ["child"], "tools": []}}}]}

    for obey in (True, False):
        hub.models.bindings.pop("parent", None)
        hub.models.bindings.pop("child", None)
        child = Child(obey)
        hub.models.register("parent", child, "parent", ["chat", "decision"])
        hub.models.register("child", child, "child", ["chat", "decision"])
        run = await hub.wait(hub.submit(flow), timeout=15)
        assert run["status"] == "succeeded", run
        result = json.loads(run["steps"][0]["output"]["text"])
        if obey:
            assert result["schema_valid"] is True and result["output"]["data"] == {"total": 5}
            assert child.attempts == 2
        else:
            # Unverified, but the child's actual answer survives alongside the errors.
            assert result["schema_valid"] is False and result["schema_errors"]
            assert result["output"]["text"].startswith("Alice totals five")
            assert result["schema_note"]


async def test_spawn_refuses_a_contract_the_child_model_cannot_meet(hub):
    """A structured result is a decision request; refuse it at spawn, not inside the child."""
    class ChatOnly:
        async def generate(self, request, model):
            return ModelResult(text="plain answer", usage={"mock": True})

    hub.models.register("chat-only", ChatOnly(), "chat-only", ["chat"])
    # The parent runs the orchestration; chat-only is the (unsuitable) child it asks for.
    flow = {"name": "unsatisfiable", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait"],
        "delegation": {"models": ["chat-only"], "tools": []}}}]}

    class Parent:
        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            return ModelResult(tool_calls=[ToolCall(id="s", name="agents.spawn", arguments={
                "goal": "count", "model": "chat-only", "tools": [],
                "response_schema": {"type": "object", "properties": {"total": {"type": "integer"}},
                                    "required": ["total"]}})], usage={"mock": True})

    hub.models.register("parent", Parent(), "parent", ["chat", "decision"])
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    assert "cannot return a structured result" in run["steps"][0]["output"]["text"]
    assert not run["children"]


async def test_a_parent_reads_a_running_childs_notes_without_waiting(hub):
    """Progress is pull-only: a long child reports as it goes and the parent reads it mid-flight.

    The child parks itself after publishing (it raises WaitingChildren) so the parent provably
    reads a note from a child that has not finished, not a note recovered after the fact.
    """
    class Parent:
        def __init__(self):
            self.reads = 0

        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            if not observations:
                return ModelResult(tool_calls=[ToolCall(id="spawn", name="agents.spawn", arguments={
                    "goal": "count the rows", "model": "child", "tools": ["agents.note"]})],
                    usage={"mock": True})
            # The child publishes on its own schedule, so the parent polls the board: each read
            # returns immediately, empty or not, and never blocks on the child finishing.
            self.reads += 1
            if self.reads == 1:
                assert observations[-1] == {"notes": [], "children": {}, "cursor": 0}, observations[-1]
                return ModelResult(tool_calls=[ToolCall(id="notes", name="agents.notes",
                                                        arguments={})], usage={"mock": True})
            for observation in observations[1:]:
                if observation.get("notes"):
                    return ModelResult(text=json.dumps(observation["notes"]), usage={"mock": True})
            return ModelResult(tool_calls=[ToolCall(id="notes", name="agents.notes",
                                                    arguments={})], usage={"mock": True})

    class Child:
        def __init__(self):
            self.published = False

        async def generate(self, request, model):
            if not self.published:
                self.published = True
                return ModelResult(tool_calls=[ToolCall(id="n", name="agents.note",
                                                        arguments={"text": "Alice totals 200.00"})],
                                   usage={"mock": True})
            await asyncio.sleep(.5)  # still working: the parent must read the note before this ends
            return ModelResult(text="child finished too", usage={"mock": True})

    flow = {"name": "progress", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait", "agents.notes"],
        "delegation": {"models": ["child"], "tools": ["agents.note"]}}}]}
    hub.models.register("parent", Parent(), "parent", ["chat"])
    hub.models.register("child", Child(), "child", ["chat"])
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    # The note arrived through agents.notes while the child was still working, with its author.
    children = run["children"]
    assert len(children) == 1 and children[0]["status"] not in ("succeeded",)
    assert json.loads(run["steps"][0]["output"]["text"])[0]["text"] == "Alice totals 200.00"
    notes = hub.delegation.notes_for(run["id"])
    assert [n["text"] for n in notes] == ["Alice totals 200.00"]
    assert notes[0]["author"] == children[0]["id"]


async def test_broadcast_reaches_siblings_and_nothing_outside_the_tree(hub):
    """One message to the caller's own children, or from a child to its siblings; never upward."""
    class Parent:
        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            if not observations:
                return ModelResult(tool_calls=[
                    ToolCall(id="s1", name="agents.spawn", arguments={"goal": "one", "model": "child", "tools": []}),
                    ToolCall(id="s2", name="agents.spawn", arguments={"goal": "two", "model": "child", "tools": []}),
                ], usage={"mock": True})
            if len(observations) == 2:
                return ModelResult(tool_calls=[ToolCall(id="b", name="agents.broadcast",
                                                        arguments={"text": "align on the same total"})],
                                   usage={"mock": True})
            if len(observations) == 3:
                return ModelResult(tool_calls=[ToolCall(id="outer", name="agents.broadcast",
                                                        arguments={"text": "leak", "to": ["not-my-child"]})],
                                   usage={"mock": True})
            return ModelResult(text="done", usage={"mock": True})

    class Child:
        async def generate(self, request, model):
            await asyncio.sleep(.5)  # still in flight when the parent broadcasts
            return ModelResult(text="child answer", usage={"mock": True})

    flow = {"name": "broadcast", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait", "agents.broadcast"],
        "delegation": {"models": ["child"], "tools": []}}}]}
    hub.models.register("parent", Parent(), "parent", ["chat"])
    hub.models.register("child", Child(), "child", ["chat"])
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    children = [c["id"] for c in run["children"]]
    assert len(children) == 2
    # The refusal is an observation, so the task continues; the message reached nobody.
    assert "reaches only the children or siblings" in run["steps"][0]["output"]["text"]
    with hub.store.connect() as db:
        inbox = [dict(r) for r in db.execute(
            "SELECT child,text,delivered FROM agent_mailbox WHERE parent=?", (run["id"],))]
    # Both children were still running, so both received it; a finished child is not a target
    # (a message to one that had already succeeded would never be read).
    assert {row["child"] for row in inbox} == set(children)
    assert {row["text"] for row in inbox} == {"align on the same total"}
    assert all(row["delivered"] == 0 for row in inbox)


async def test_max_active_suspends_a_spawn_instead_of_refusing_it(hub):
    """A concurrency ceiling is back-pressure: the third spawn waits, it is not answered with no."""
    class Parent:
        def __init__(self):
            self.spawn_attempts = 0

        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            if not observations:
                return ModelResult(tool_calls=[
                    ToolCall(id="s1", name="agents.spawn", arguments={"goal": "one", "model": "slow", "tools": []}),
                    ToolCall(id="s2", name="agents.spawn", arguments={"goal": "two", "model": "slow", "tools": []}),
                    ToolCall(id="s3", name="agents.spawn", arguments={"goal": "three", "model": "slow", "tools": []}),
                ], usage={"mock": True})
            if len(observations) >= 3:
                # Every spawn eventually returned an id; none of them was refused.
                assert all("id" in o and "error" not in o for o in observations[:3]), observations
                return ModelResult(text="all three spawned", usage={"mock": True})
            return ModelResult(text="waiting", usage={"mock": True})

    class Slow:
        async def generate(self, request, model):
            await asyncio.sleep(.15)
            return ModelResult(text="slow child", usage={"mock": True})

    hub.models.register("parent", Parent(), "parent", ["chat"])
    hub.models.register("slow", Slow(), "slow", ["chat"])
    flow = {"name": "ceiling", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait"],
        "delegation": {"models": ["slow"], "tools": [], "max_active": 2}}}]}
    run = await hub.wait(hub.submit(flow), timeout=20)
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["output"]["text"] == "all three spawned"
    assert len(run["children"]) == 3
    assert run["usage"]["child_runs"] == 3



async def test_omitted_tools_inherit_the_parent_grant_and_a_named_list_narrows_it(hub):
    """The live failure, pinned: a spawned child must not arrive helpless.

    A child handed no tools can only reason and reply, so a research goal sent that way burns a
    whole child run reporting what it lacks. Omitting tools therefore inherits exactly what the
    parent was granted - never more - and an explicit list still narrows it.
    """
    seen = {}

    class Parent:
        def __init__(self):
            self.spawned = 0

        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            if not observations:
                # First spawn names no tools; the second narrows to a single one. Both go out in one
                # turn, which is also the batching discipline the operator prompt asks for.
                return ModelResult(tool_calls=[
                    ToolCall(id="s1", name="agents.spawn", arguments={"goal": "no tools named", "model": "child"}),
                    ToolCall(id="s2", name="agents.spawn", arguments={
                        "goal": "narrowed", "model": "child", "tools": ["web.search"]}),
                ], usage={"mock": True})
            ids = [o["id"] for o in observations if "id" in o]
            if len(observations) < 3:
                # Wait on both: a child that is never waited on is never driven, so this is what
                # makes the child's own generate() run at all.
                return ModelResult(tool_calls=[
                    ToolCall(id=f"w{i}", name="agents.wait", arguments={"id": child})
                    for i, child in enumerate(ids)
                ], usage={"mock": True})
            return ModelResult(text="both spawned", usage={"mock": True})

    class Child:
        async def generate(self, request, model):
            seen[request.messages[1]["content"]] = {t.name for t in request.tools}
            return ModelResult(text="child answer", usage={"mock": True})

    def until(predicate, timeout=10):
        async def wait():
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(.01)
        return wait

    # The parent step must actually hold what a child inherits: nothing validates that a grant is a
    # subset of the parent's own tools, and a grant naming more is a real, reachable configuration.
    inherited_tools = ["web.search", "web.read"]
    outside_grant = "backend.terminal"
    flow = {"name": "inherit", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait", *inherited_tools, outside_grant],
        "delegation": {"models": ["child"], "tools": [*inherited_tools, outside_grant]}}}]}
    hub.models.register("parent", Parent(), "parent", ["chat"])
    hub.models.register("child", Child(), "child", ["chat"])
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    await until(lambda: len(seen) == 2)()

    inherited = seen["no tools named"]
    # The whole grant, since the parent holds all of it, plus the two a child always gets.
    assert inherited == {*inherited_tools, outside_grant, "agents.reply", "agents.note"}
    narrowed = seen["narrowed"]
    assert narrowed == {"web.search", "agents.reply", "agents.note"}


async def test_inheritance_never_exceeds_what_the_parent_step_holds(hub):
    """A grant may name more than the parent holds; the child must not receive that extra power.

    Nothing validates that a delegation grant is a subset of the parent step's own tools, so a
    grant listing a capability the parent lacks is reachable. Inheriting it verbatim would trip the
    authority checks downstream (development/code grants the parent does not hold) and abort a
    spawn that should have succeeded with the smaller, honest set.
    """
    seen = {}

    class Parent:
        async def generate(self, request, model):
            observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
            if observations and isinstance(observations[-1].get("error"), dict):
                return ModelResult(text="refused: " + observations[-1]["error"]["message"], usage={"mock": True})
            if not observations:
                return ModelResult(tool_calls=[ToolCall(id="s", name="agents.spawn",
                                                        arguments={"goal": "no tools named", "model": "child"})],
                                   usage={"mock": True})
            if len(observations) == 1:
                return ModelResult(tool_calls=[ToolCall(id="w", name="agents.wait",
                                                        arguments={"id": observations[0]["id"]})], usage={"mock": True})
            return ModelResult(text=observations[-1]["output"]["text"], usage={"mock": True})

    class Child:
        async def generate(self, request, model):
            seen["tools"] = {t.name for t in request.tools}
            return ModelResult(text="child answer", usage={"mock": True})

    flow = {"name": "narrowed grant", "steps": [{"id": "agent", "kind": "agent", "target": "parent", "input": {
        "prompt": "delegate", "tools": ["agents.spawn", "agents.wait", "web.search"],
        # development.* is granted but the parent step does not hold it: inheriting it would raise.
        "delegation": {"models": ["child"], "tools": ["web.search", "development.save_workflow"]}}}]}
    hub.models.register("parent", Parent(), "parent", ["chat"])
    hub.models.register("child", Child(), "child", ["chat"])
    run = await hub.wait(hub.submit(flow), timeout=15)
    assert run["status"] == "succeeded", run
    assert seen["tools"] == {"web.search", "agents.reply", "agents.note"}
    assert "development.save_workflow" not in seen["tools"]
