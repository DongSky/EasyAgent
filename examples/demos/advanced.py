"""Complete chains: knowledge -> roles -> artifacts, loops, triggers and evaluation."""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from easyagent.contracts import EvaluationSuite, ModelResult, Policy
from easyagent.runtime import Hub
from easyagent.scheduling import Trigger


class PlannerFixture:
    async def generate(self, request, model):
        done = any("Observed tool result" in m["content"] for m in request.messages)
        data = {"done": done, "answer": "Verified fixture result" if done else "", "plan": [] if done else [
            {"tool": "core.echo", "arguments": {"receipt": "fixture"}, "purpose": "verify the receipt"}]}
        return ModelResult(text=json.dumps(data), data=data, usage={"mock": True})


async def main():
    with tempfile.TemporaryDirectory(prefix="eah-advanced-") as directory:
        hub = Hub(Path(directory) / "hub.db", poll_seconds=0.02)
        hub.models.register("planner", PlannerFixture(), "planner-protocol-fixture", {"decision"})
        await hub.knowledge.ingest("home", "Move notice", "demo-notice", "Move date: 2026-10-01. Reserve elevator three days early.", "mock")
        await hub.start()
        try:
            workflow = {"name": "complete chain", "steps": [
                {"id": "context", "kind": "retrieve", "input": {"namespace": "home", "query": "elevator", "mode": "hybrid"}},
                {"id": "planner", "kind": "agent", "target": "planner", "depends_on": ["context"], "input": {"strategy": "plan_execute", "prompt": "Verify task completion", "tools": ["core.echo"], "knowledge": ["home"]}},
                {"id": "reviewer", "kind": "agent", "target": "mock", "depends_on": ["planner"], "input": {"instructions": "Review the planner result", "prompt": {"$ref": "planner.text"}}},
                {"id": "batch", "kind": "foreach", "depends_on": ["reviewer"], "input": {"items": ["check dates", "check owners"]}, "body": {
                    "name": "verification", "steps": [{"id": "result", "kind": "transform", "input": {"task": {"$ref": "$input.item"}}}]}},
                {"id": "report", "kind": "artifact", "depends_on": ["batch"], "input": {"name": "report.json", "content": {"$ref": "batch.results"}, "media_type": "application/json"}},
            ]}
            run = await hub.wait(hub.submit(workflow))
            assert run["status"] == "succeeded", run
            policy = hub.evolution.propose(Policy(name="evidence", model="mock", instructions="Preserve evidence", tools=["core.echo"]))
            await hub.evolution.evaluate(policy["id"], EvaluationSuite(cases=[{"prompt": '{"tool":"core.echo","arguments":{"text":"evidence"}}', "expected": "evidence", "expected_tools": ["core.echo"], "forbidden": ["invented"]}]))
            hub.evolution.activate(policy["id"])
            hook = hub.scheduler.create(Trigger(name="event", kind="webhook", workflow={"name": "event demo", "steps": [{"id": "input", "kind": "transform", "input": {"value": {"$ref": "$input"}}}]}))
            triggered = hub.scheduler.fire(hook["id"], {"event": "notice arrived"}, "notice-1")
            assert hub.scheduler.fire(hook["id"], {"event": "notice arrived"}, "notice-1") == triggered
            assert (await hub.wait(triggered))["status"] == "succeeded"
            hub.scheduler.create(Trigger(name="one time", kind="schedule", workflow={"name": "scheduled demo", "steps": [{"id": "hello", "target": "core.echo"}]}, start_at=time.time()-1))
            hub.scheduler.tick()
            output = {"status": "passed", "model_evidence": "deterministic fixtures only", "main_run": run["id"],
                "chain": [s["kind"] for s in workflow["steps"]], "usage": run["usage"],
                "artifacts": hub.artifacts.list(run["id"]), "child_runs": len(run["children"]),
                "policy": hub.evolution.active("evidence")["id"], "webhook_deduplication": "passed", "schedule": "submitted"}
            print(json.dumps(output, ensure_ascii=False, indent=2))
        finally:
            await hub.stop()


if __name__ == "__main__":
    asyncio.run(main())
