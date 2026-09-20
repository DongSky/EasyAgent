import json
import pytest
from easyagent.contracts import ModelResult, ToolSpec


def flow(value):
    return {"name": "count", "steps": [{"id": "answer", "kind": "transform", "input": {"count": value}}]}


class RepairModel:
    async def generate(self, request, model):
        return ModelResult(
            data={
                "action": "revise",
                "reason": "add missing count",
                "workflow": flow(3),
                "question": "",
                "code_candidate": None,
            },
            usage={"mock": True},
        )


async def test_goal_repairs_business_result_and_preserves_budget(hub):
    hub.models.register("repair", RepairModel(), "fixture", ["decision"])
    goal = hub.goals.create(
        {
            "objective": "count must be 3",
            "workflow": flow(1),
            "model": "repair",
            "semantic_check": False,
            "checks": [{"description": "three items", "path": "answer.count", "schema": {"const": 3}}],
        }
    )
    r = await hub.wait(goal["id"])
    assert r["status"] == "succeeded", r
    assert r["steps"][0]["output"]["outputs"]["answer"]["count"] == 3
    assert len(r["children"]) == 2 and r["usage"]["model_calls"] == 1
    assert [x["verdict"]["passed"] for x in r["steps"][0]["output"]["history"]] == [False, True]
    assert any(e["kind"] == "goal.revised" for e in hub.store.events(r["id"]))
    hub.goals.control(r["id"], "feedback", "please verify again")
    again = await hub.wait(r["id"])
    assert again["usage"]["model_calls"] > r["usage"]["model_calls"]


async def test_goal_stops_stagnation_and_permission_escalation(hub):
    class Same:
        async def generate(self, request, model):
            return ModelResult(
                data={"action": "revise", "reason": "same", "workflow": flow(1)}, usage={"mock": True}
            )

    hub.models.register("same", Same(), "fixture", ["decision"])
    r = await hub.wait(
        hub.goals.create(
            {
                "objective": "three",
                "workflow": flow(1),
                "model": "same",
                "semantic_check": False,
                "checks": [{"description": "count", "path": "answer.count", "schema": {"const": 3}}],
            }
        )["id"]
    )
    assert r["status"] == "failed" and r["steps"][0]["output"]["goal_status"] == "incomplete"
    assert "repeated" in r["steps"][0]["output"]["reason"]
    with pytest.raises(PermissionError):
        hub.goals.create(
            {
                "objective": "bad",
                "model": "same",
                "workflow": {"name": "bad", "steps": [{"id": "t", "target": "core.echo"}]},
            }
        )


async def test_goal_confirmed_write_not_repeated_after_replan(hub):
    writes = []

    async def write(args, ctx):
        writes.append(args)
        return {"receipt": "one"}

    hub.tools.register(ToolSpec(name="test.write", effect="write", idempotent=True), write)
    initial = flow(1)
    initial["steps"].insert(0, {"id": "write", "target": "test.write", "input": {}})

    class Fix:
        async def generate(self, request, model):
            current = json.loads(request.messages[-1]["content"])["workflow"]
            current["steps"][-1]["input"] = {"count": 3}
            return ModelResult(
                data={"action": "revise", "reason": "fix count", "workflow": current}, usage={"mock": True}
            )

    hub.models.register("fix", Fix(), "fixture", ["decision"])
    g = hub.goals.create(
        {
            "objective": "write once and count three",
            "workflow": initial,
            "model": "fix",
            "allowed_tools": ["test.write"],
            "semantic_check": False,
            "checks": [{"description": "count", "path": "answer.count", "schema": {"const": 3}}],
        }
    )
    r = await hub.wait(g["id"])
    assert r["status"] == "waiting_approval", r
    hub.tools.approve(hub.store, r["approvals"][0]["id"], True)
    r = await hub.wait(g["id"])
    assert r["status"] == "succeeded", r
    assert len(writes) == 1
    saved = hub.development.get("workflow", r["steps"][0]["state"]["saved_workflow"]["id"])
    assert saved["revision"] == 2
    assert saved["workflow"]["steps"][0]["kind"] == "tool"
    from easyagent.workflow_packages import export_workflow, import_workflow

    package = export_workflow(hub, saved["workflow"])
    imported = import_workflow(hub, {"package": package.model_dump()})
    again = await hub.wait(hub.submit(imported["workflow"]))
    assert again["status"] == "waiting_approval"
    hub.tools.approve(hub.store, again["approvals"][0]["id"], True)
    assert (await hub.wait(again["id"]))["status"] == "succeeded"
    assert len(writes) == 2  # A new project must perform its own write.
    with pytest.raises(ValueError, match="saved workflow"):
        export_workflow(hub, r["spec"])


async def test_goal_failure_repair_and_restart_wait(hub, tmp_path):
    from easyagent.runtime import Hub

    async def broken(args, ctx):
        if args["value"] < 0:
            raise ValueError("value must be nonnegative")
        return {"count": args["value"]}

    hub.tools.register(
        ToolSpec(
            name="test.number",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        ),
        broken,
    )

    class RepairFailure:
        async def generate(self, request, model):
            data = json.loads(request.messages[-1]["content"])
            if not data.get("feedback"):
                return ModelResult(
                    data={
                        "action": "need_input",
                        "question": "Which value should I use?",
                        "reason": "missing requirement",
                    },
                    usage={"mock": True},
                )
            return ModelResult(
                data={
                    "action": "revise",
                    "reason": "use clarified value",
                    "workflow": {
                        "name": "fixed",
                        "steps": [{"id": "answer", "target": "test.number", "input": {"value": 3}}],
                    },
                },
                usage={"mock": True},
            )

    hub.models.register("repair-input", RepairFailure(), "fixture", ["decision"])
    run = await hub.wait(
        hub.goals.create(
            {
                "objective": "find a nonnegative count",
                "model": "repair-input",
                "workflow": {
                    "name": "failure",
                    "steps": [{"id": "answer", "target": "test.number", "input": {"value": -1}}],
                },
                "allowed_tools": ["test.number"],
                "semantic_check": False,
                "checks": [{"description": "count", "path": "answer.count", "schema": {"const": 3}}],
            }
        )["id"]
    )
    assert run["status"] == "waiting_input", run
    await hub.stop()
    restart = Hub(hub.store.path, poll_seconds=0.01)
    restart.tools.register(hub.tools.spec("test.number"), broken)
    restart.models.register("repair-input", RepairFailure(), "fixture", ["decision"])
    await restart.start()
    try:
        restart.goals.control(run["id"], "feedback", "Use three")
        result = await restart.wait(run["id"])
        assert result["status"] == "succeeded", result
        assert result["usage"]["model_calls"] == 2
    finally:
        await restart.stop()


async def test_goal_adds_pure_code_and_verifies_result(hub):
    class CodeRepair:
        async def generate(self, request, model):
            data = json.loads(request.messages[-1]["content"])
            name = data["code_namespace"]
            code = {
                "manifest": {
                    "id": name,
                    "revision": 1,
                    "title": "sum",
                    "tools": [
                        {
                            "spec": {
                                "name": name + ".sum",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                                    "required": ["a", "b"],
                                },
                                "output_schema": {
                                    "type": "object",
                                    "properties": {"count": {"type": "number"}},
                                    "required": ["count"],
                                },
                            },
                            "handler": "sum",
                        }
                    ],
                },
                "files": {
                    "extension.js": 'function handle(r){if(r.method==="sum")return {result:{count:r.params.a+r.params.b}};return {result:{}};}'
                },
                "scenarios": [{"tool": name + ".sum", "input": {"a": 2, "b": 1}, "expected": {"count": 3}}],
            }
            return ModelResult(
                data={
                    "action": "revise",
                    "reason": "add a tested sum node",
                    "workflow": {
                        "name": "new capability",
                        "steps": [{"id": "answer", "target": name + ".sum", "input": {"a": 4, "b": 3}}],
                    },
                    "code_candidate": code,
                },
                usage={"mock": True},
            )

    hub.models.register("code-repair", CodeRepair(), "fixture", ["decision"])
    r = await hub.wait(
        hub.goals.create(
            {
                "objective": "sum 4 and 3",
                "workflow": flow(0),
                "model": "code-repair",
                "allow_code": True,
                "semantic_check": False,
                "checks": [{"description": "sum", "path": "answer.count", "schema": {"const": 7}}],
            }
        )["id"]
    )
    assert r["status"] == "succeeded", r
    assert r["steps"][0]["output"]["outputs"]["answer"]["count"] == 7
    assert hub.code.list()[0]["status"] == "published"


async def test_goal_http_control_and_recovery_after_final_verdict(api):
    import httpx
    from easyagent.store import encode

    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        created = await client.post(
            "/v1/goals",
            json={
                "objective": "count three",
                "workflow": flow(3),
                "model": "mock",
                "max_revisions": 0,
                "semantic_check": False,
                "checks": [{"description": "count", "path": "answer.count", "schema": {"const": 3}}],
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["id"]
        result = await hub.wait(identifier)
        assert result["status"] == "succeeded", result
        resumed = await client.post("/v1/goals/" + identifier + "/control", json={"action": "resume"})
        assert resumed.status_code == 200, resumed.text
        await hub.stop()
        state = result["steps"][0]["state"]
        assert state["phase"] == "repair" and state["verdict"]["passed"]
        # Simulate process loss between durable verification and final root completion.
        with hub.store.transaction() as db:
            db.execute(
                "UPDATE steps SET status='queued',output=NULL,state=?,ready_at=0 WHERE run_id=?",
                (encode(state), identifier),
            )
            db.execute("UPDATE runs SET status='queued' WHERE id=?", (identifier,))
        await hub.start()
        recovered = await hub.wait(identifier)
        assert recovered["status"] == "succeeded", recovered
        assert recovered["usage"] == result["usage"]
        assert len(hub.development.list_versions("workflow", state["saved_workflow"]["id"])) == 1


async def test_goal_repair_cannot_switch_service_bindings(hub):
    class Switch:
        async def generate(self, request, model):
            plan = flow(3)
            plan["metadata"] = {"voice_settings": {"base_url": "https://ungranted.example"}}
            return ModelResult(
                data={"action": "revise", "reason": "change endpoint", "workflow": plan}, usage={"mock": True}
            )

    hub.models.register("switch", Switch(), "fixture", ["decision"])
    result = await hub.wait(
        hub.goals.create(
            {
                "objective": "three",
                "model": "switch",
                "workflow": flow(1),
                "semantic_check": False,
                "checks": [{"description": "count", "path": "answer.count", "schema": {"const": 3}}],
            }
        )["id"]
    )
    assert result["status"] == "waiting_input", result
    assert "cannot switch voice_settings" in result["steps"][0]["state"]["question"]
    assert len(result["children"]) == 1
