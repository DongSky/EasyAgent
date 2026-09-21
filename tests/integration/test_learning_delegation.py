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
