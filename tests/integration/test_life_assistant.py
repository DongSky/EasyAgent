from datetime import date, timedelta

import httpx
import pytest

from conftest import live_server
from easyagent.api import create_app
from easyagent.contracts import ModelResult
from easyagent.runtime import Hub
from easyagent_app import mount_app
from examples.life_assistant.app import LifeStore, TEMPLATES, install_life


@pytest.fixture
async def life_api(hub):
    app = create_app(hub, manage_workers=False)
    install_life(app, hub)
    mount_app(app)
    async with live_server(app) as url, httpx.AsyncClient(base_url=url, timeout=30) as client:
        yield client, hub


async def create_project(client, hub, kind="moving"):
    draft = await client.post("/v1/life/plans", json={"kind": kind, "title": "集成演示 · " + kind, "anchor": "2026-10-19"})
    assert draft.status_code == 201
    run_id = draft.json()["id"]
    assert (await hub.wait(run_id))["status"] == "succeeded"
    confirmed = await client.post("/v1/life/projects", json={"run_id": run_id})
    assert confirmed.status_code == 201, confirmed.text
    assert (await client.post("/v1/life/projects", json={"run_id": run_id})).json()["id"] == confirmed.json()["id"]
    return confirmed.json()


@pytest.mark.parametrize("kind", list(TEMPLATES))
async def test_every_life_template_to_completion_receipts_and_restart(life_api, kind):
    client, hub = life_api
    assert (await client.get("/life")).status_code == 200
    assert (await client.get("/life-assets/app.js")).status_code == 200
    project = await create_project(client, hub, kind)
    assert len(project["tasks"]) == len(TEMPLATES[kind]["tasks"])
    for task in project["tasks"]:
        result = await client.patch("/v1/life/tasks/" + task["id"], json={"state": "done", "evidence": "集成夹具回执 · " + task["task_key"], "expected_version": task["version"]})
        assert result.status_code == 200, result.text
    result = result.json()
    assert all(t["state"] == "done" and t["evidence"] for t in result["tasks"])
    await hub.stop()
    restarted = Hub(hub.store.path)
    persisted = LifeStore(restarted).get(project["id"])
    assert persisted == result


async def test_moving_collaboration_dependencies_reschedule_costs_and_conflicts(life_api):
    client, hub = life_api
    project = await create_project(client, hub)
    tasks = {t["task_key"]: t for t in project["tasks"]}
    def path(key):
        return "/v1/life/tasks/" + tasks[key]["id"]
    assert (await client.patch(path("lease"), json={"state": "done"})).status_code == 422
    assert (await client.patch(path("booking"), json={"state": "done", "evidence": "too soon"})).status_code == 409
    assert (await client.patch(path("lease"), json={"state": "waiting"})).status_code == 422
    assert (await client.patch(path("lease"), json={"state": "waiting", "waiting_on": "房东", "owner": "小林", "expected_version": 1})).status_code == 200
    assert (await client.patch(path("lease"), json={"note": "stale", "expected_version": 1})).status_code == 409
    assert (await client.patch(path("internet"), json={"state": "blocked", "note": "服务商暂未提供可约时间"})).status_code == 200
    assert (await client.patch(path("lease"), json={"state": "done", "evidence": "房东确认退租"})).status_code == 200
    assert (await client.patch(path("quotes"), json={"state": "done", "evidence": "三份报价已核对", "cost_cents": 150000, "currency": "CNY"})).status_code == 200
    assert (await client.patch(path("lease"), json={"state": "todo"})).status_code == 409
    changed = await client.patch(f"/v1/life/projects/{project['id']}/date", json={"anchor": "2026-10-26"})
    assert changed.status_code == 200
    for task in changed.json()["tasks"]:
        old = tasks[task["task_key"]]
        expected = old["due"] if task["state"] == "done" else (date.fromisoformat(old["due"]) + timedelta(days=7)).isoformat()
        assert task["due"] == expected
    resource = await client.post(f"/v1/life/projects/{project['id']}/resources", json={"kind": "contact", "title": "物业联系", "value": "synthetic-contact"})
    assert resource.json()["resources"][0]["value"] == "synthetic-contact"
    reminders = (await client.get("/v1/life/reminders?today=2026-10-31")).json()
    assert reminders and all(t["state"] != "done" for t in reminders)


@pytest.mark.parametrize("mode", ["manual", "model", "invalid_date", "invented_source"])
async def test_materials_confirm_idempotency_unknown_dates_and_atomic_rejection(life_api, mode):
    client, hub = life_api
    project = await create_project(client, hub, "school")
    text = "周五需要带水壶，接送人尚未确认。"
    class ExtractFixture:
        async def generate(self, request, model):
            return ModelResult(data={"tasks": [
                {"title": "准备水壶", "due": None, "owner": "待分工", "source": "需要带水壶"},
                {"title": "确认接送人", "due": "2026-02-31" if mode == "invalid_date" else None,
                 "owner": "待分工", "source": "并不存在的约定" if mode == "invented_source" else "接送人尚未确认"}]})
    hub.models.register("extract-fixture", ExtractFixture(), "synthetic", {"decision"})
    response = await client.post(f"/v1/life/projects/{project['id']}/extract", json={"text": text, "model": "template" if mode == "manual" else "extract-fixture"})
    assert response.status_code == 200
    run_id = response.json()["id"]
    assert (await hub.wait(run_id))["status"] == "succeeded"
    before = len(project["tasks"])
    path = f"/v1/life/projects/{project['id']}/extract/confirm"
    confirmed = await client.post(path, json={"run_id": run_id})
    if mode in ("invalid_date", "invented_source"):
        assert confirmed.status_code == 422, confirmed.text
        assert len((await client.get(f"/v1/life/projects/{project['id']}")).json()["tasks"]) == before
    else:
        assert confirmed.status_code == 200, confirmed.text
        assert len(confirmed.json()["tasks"]) == before + (1 if mode == "manual" else 2)
        imported = [t for t in confirmed.json()["tasks"] if t["task_key"].startswith(run_id)]
        assert all(t["due"] is None and t["offset_days"] is None for t in imported)
        assert len((await client.post(path, json={"run_id": run_id})).json()["tasks"]) == len(confirmed.json()["tasks"])
        changed = (await client.patch(f"/v1/life/projects/{project['id']}/date", json={"anchor": "2026-11-01"})).json()
        assert all(t["due"] is None for t in changed["tasks"] if t["task_key"].startswith(run_id))
    resources = (await client.get(f"/v1/life/projects/{project['id']}")).json()["resources"]
    assert resources[0]["value"] == text
