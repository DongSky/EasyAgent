import asyncio
import json
import time

import httpx

from conftest import live_server
from easyagent.api import create_app
from easyagent.runtime import Hub


DEVICE = "a" * 32
OTHER_DEVICE = "b" * 32


async def test_system_notification_workflow_approval_device_binding_claim_and_restart(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        config = {"id": "desktop", "title": "系统通知", "kind": "system", "device_id": DEVICE}
        response = await client.post("/v1/connections", json=config)
        assert response.status_code == 201, response.text
        assert response.json()["url"] == "" and not response.json()["credential"]
        workflow = hub.prepare({
            "name": "system notice",
            "steps": [{"id": "notify", "target": "connection.desktop", "input": {"text": "任务已完成"}}],
        })
        # An already prepared workflow keeps its original receiving browser.
        assert (await client.post("/v1/connections", json={**config, "device_id": OTHER_DEVICE})).status_code == 201
        run = await hub.wait(hub.submit(workflow))
        assert run["status"] == "waiting_approval"
        assert hub.connections.deliveries() == []
        await client.post("/v1/approvals/" + run["approvals"][0]["id"], json={"approved": True})
        run = await hub.wait(run["id"])
        assert run["status"] == "succeeded"
        assert run["steps"][0]["output"]["delivered"] is False
        await hub.connections.tick()
        assert hub.connections.deliveries()[0]["status"] == "queued"

    # Unclaimed queue and pinned connector config survive constructing a fresh Hub.
    restored = Hub(hub.store.path)
    async with live_server(create_app(restored, manage_workers=False)) as restarted_url:
        async with httpx.AsyncClient(base_url=restarted_url) as client:
            assert (await client.post("/v1/connections/system/claim", json={"device_id": OTHER_DEVICE})).json() == {"notification": None}
            claims = await asyncio.gather(*[
                client.post("/v1/connections/system/claim", json={"device_id": DEVICE}) for _ in range(2)
            ])
            messages = [r.json()["notification"] for r in claims if r.json()["notification"]]
            assert len(messages) == 1  # Multiple tabs cannot receive the same item.
            message = messages[0]
            assert message["text"] == "任务已完成" and message["title"] == "系统通知"
            path = "/v1/connections/system/" + message["id"] + "/receipt"
            body = {"device_id": DEVICE, "claim_token": message["claim_token"], "outcome": "submitted"}
            assert (await client.post(path, json={**body, "device_id": OTHER_DEVICE})).status_code == 409
            assert (await client.post(path, json={**body, "claim_token": "0" * 32})).status_code == 409
            with hub.store.connect() as db:
                db.execute("UPDATE deliveries SET next_at=? WHERE id=?", (time.time() - 100, message["id"]))
            await restored.connections.tick()
            assert restored.connections.deliveries()[0]["status"] == "uncertain"
            assert (await client.post("/v1/connections/system/claim", json={"device_id": DEVICE})).json() == {"notification": None}
            # A delayed acknowledgement may resolve uncertainty, but never resend the action.
            acknowledged = (await client.post(path, json=body)).json()
            assert acknowledged["status"] == "submitted"
            assert acknowledged["receipt"]["display_confirmed"] is False
            assert acknowledged["receipt"]["read_confirmed"] is False
            assert (await client.post(path, json=body)).json() == acknowledged
            assert (await client.post(path, json={**body, "outcome": "failed"})).status_code == 409
            assert restored.connections.deliveries()[0]["attempts"] == 1


async def test_system_notification_settings_validation_and_test_failure_receipt(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        config = {"id": "browser", "title": "本机提醒", "kind": "system", "device_id": DEVICE}
        for changes in [{"device_id": None}, {"url": "https://example.com"}, {"credential": "secret"}, {"idempotent": True}]:
            assert (await client.post("/v1/connections", json={**config, **changes})).status_code == 422
        assert (await client.post("/v1/connections", json={"id": "web", "title": "web", "kind": "webhook"})).status_code == 422
        await client.post("/v1/connections", json=config)
        assert (await client.post("/v1/connections/browser/test-system", json={"device_id": OTHER_DEVICE})).status_code == 409
        result = await client.post("/v1/connections/browser/test-system", json={"device_id": DEVICE})
        assert result.status_code == 201 and result.json()["delivered"] is False
        claim = (await client.post("/v1/connections/system/claim", json={"device_id": DEVICE})).json()["notification"]
        response = await client.post("/v1/connections/system/" + claim["id"] + "/receipt", json={
            "device_id": DEVICE, "claim_token": claim["claim_token"],
            "outcome": "failed", "reason": "permission_denied",
        })
        assert response.json()["status"] == "failed"
        await hub.connections.tick()
        delivery = hub.connections.deliveries()[0]
        assert delivery["status"] == "failed" and delivery["attempts"] == 1
        assert "权限" in delivery["error"]
        assert json.loads(delivery["receipt"])["display_confirmed"] is False
        assert (await client.post("/v1/connections/system/claim", json={"device_id": DEVICE})).json() == {"notification": None}
