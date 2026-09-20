import json

import httpx
import pytest

from easyagent.contracts import ModelResult, ToolCall
from easyagent.runtime import Hub
from easyagent.skill_packages import builtin_skills


async def test_skill_install_snapshot_resources_updates_and_share(api, tmp_path):
    url, hub = api
    p = builtin_skills()[0]["package"]
    async with httpx.AsyncClient(base_url=url) as c:
        r = await c.post("/v1/skill-packages/install", json={"package": p})
        assert r.status_code == 200, r.text
        name = r.json()["name"]
        assert name in [s["name"] for s in (await c.get("/v1/skills")).json()]

        class Reader:
            async def generate(self, request, model):
                if not any(m["role"] == "tool" for m in request.messages):
                    return ModelResult(
                        tool_calls=[
                            ToolCall(
                                id="read",
                                name="skills.read",
                                arguments={"name": name, "path": "references/check.md"},
                            )
                        ]
                    )
                return ModelResult(text=request.messages[-1]["content"])

        hub.models.register("skill-reader", Reader(), "reader", ["chat"])
        frozen = hub.prepare(
            {
                "name": "snapshot",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "skill-reader",
                        "input": {"prompt": "check", "skills": [name], "tools": ["skills.read"]},
                    }
                ],
            }
        )
        old = p["files"]["references/check.md"]
        p["files"]["references/check.md"] = "new instructions"
        assert (
            await c.post("/v1/skill-packages/install", json={"package": p, "expected_revision": 1})
        ).status_code == 200
        assert (
            await c.post("/v1/skill-packages/install", json={"package": p, "expected_revision": 1})
        ).status_code == 409
        await c.post(f"/v1/skill-packages/{name}/activate", json={"revision": None})
        result = await hub.wait(hub.submit(frozen))
        assert json.loads(result["steps"][0]["output"]["text"])["text"] == old
        exported = (await c.get(f"/v1/skill-packages/{name}?revision=1")).json()["package"]
        fresh = Hub(tmp_path / "new.db")
        fresh.skill_packages.install({"package": exported})
        restarted = Hub(fresh.store.path)
        assert restarted.skills.resource(name, "references/check.md") == old
        await c.post(f"/v1/skill-packages/{name}/activate", json={"revision": 1})
        conv = await hub.conversations.create({"model": "skill-reader"})
        turn = await hub.conversations.send(conv["id"], {"text": "/" + name + " check"})
        run = await hub.wait(turn["run_id"])
        assert run["status"] == "succeeded"


async def test_skill_source_pin_and_invalid_packages(api, monkeypatch):
    url, hub = api
    p = builtin_skills()[1]["package"]
    name = "travel-check"
    sha = "a" * 40

    async def handler(req):
        path = req.url.path
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": sha})
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": f"skills/{name}/" + k, "type": "blob", "mode": "100644", "size": len(v)}
                        for k, v in p["files"].items()
                    ]
                },
            )
        key = path.split("/" + sha + "/skills/" + name + "/")[1]
        return httpx.Response(200, text=p["files"][key])

    original = httpx.AsyncClient

    def client(*a, **kw):
        if not kw.get("base_url"):
            kw["transport"] = httpx.MockTransport(handler)
        return original(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    source = await hub.skill_packages.remote({"repository": "owner/repo", "path": "skills/travel-check"})
    assert source["source"]["ref"] == sha
    hub.skill_packages.install({"package": source["package"]})
    for path in ["../escape", "/absolute", "x/../../y"]:
        with pytest.raises(ValueError):
            hub.skill_packages.install({"package": {"files": p["files"] | {path: "bad"}}})
    bad = {"files": p["files"] | {"SKILL.md": "not a skill"}}
    with pytest.raises(ValueError):
        hub.skill_packages.install({"package": bad})


async def test_skill_draft_preview_install_and_on_demand_read(api):
    url, hub = api
    package = builtin_skills()[0]["package"]

    class Author:
        async def generate(self, request, model):
            return ModelResult(data=package, usage={"mock": True})

    hub.models.register("skill-author", Author(), "fixture", ["decision"])
    async with httpx.AsyncClient(base_url=url) as client:
        response = await client.post(
            "/v1/skill-drafts", json={"requirement": "Moving checklist", "model": "skill-author"}
        )
        assert response.status_code == 201, response.text
        identifier = response.json()["id"]
        assert (await hub.wait(identifier))["status"] == "succeeded"
        draft = (await client.get("/v1/skill-drafts/" + identifier)).json()
        assert draft["status"] == "ready"
        assert not hub.skill_packages.list()  # Generating a draft never publishes it.
        installed = (
            await client.post("/v1/skill-packages/install", json={"package": draft["package"]})
        ).json()
        name = installed["name"]

    class Reader:
        async def generate(self, request, model):
            if not any(m["role"] == "tool" for m in request.messages):
                assert name in request.messages[0]["content"]
                assert package["files"]["SKILL.md"] not in request.messages[0]["content"]
                return ModelResult(
                    tool_calls=[ToolCall(id="read", name="skills.read", arguments={"name": name})]
                )
            return ModelResult(text=request.messages[-1]["content"])

    hub.models.register("ondemand", Reader(), "fixture", ["chat"])
    run = await hub.wait(
        hub.submit(
            {
                "name": "On-demand skill",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "ondemand",
                        "input": {
                            "prompt": "Read the method",
                            "tools": ["skills.read"],
                            "skill_access": [name],
                        },
                    }
                ],
            }
        )
    )
    assert run["status"] == "succeeded", run
    assert json.loads(run["steps"][0]["output"]["text"])["text"] == package["files"]["SKILL.md"]
    from easyagent.workflow_packages import export_workflow, import_workflow

    exported = export_workflow(hub, run["spec"])
    hub.skill_packages.activate(name, None)
    imported = import_workflow(hub, {"package": exported.model_dump()})
    again = await hub.wait(hub.submit(imported["workflow"]))
    assert again["status"] == "succeeded", again
