import asyncio
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response
from conftest import live_server
from easyagent.runtime import Hub
from easyagent.scheduling import Trigger


async def test_connector_approval_vault_version_retry_receipt_and_restart(api, tmp_path):
    url, hub = api
    remote = FastAPI()
    calls = []

    @remote.api_route("/{path:path}", methods=["POST", "PUT"])
    async def receive(path: str, request: Request):
        calls.append((path, dict(request.headers), await request.body()))
        return Response(status_code=503 if len(calls) == 1 else 201, headers={"ETag": "receipt-1"})

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        assert (
            await client.put("/v1/connections/credentials/testkey", json={"value": "secret-value-123"})
        ).status_code == 200
        data = {
            "id": "notice",
            "title": "Notify",
            "kind": "webhook",
            "url": endpoint + "/old",
            "credential": "testkey",
            "idempotent": True,
        }
        assert (await client.post("/v1/connections", json=data)).status_code == 201
        flow = hub.prepare(
            {
                "name": "send",
                "steps": [{"id": "send", "target": "connection.notice", "input": {"text": "hello"}}],
            }
        )
        await client.post("/v1/connections", json={**data, "url": endpoint + "/new"})
        run = await hub.wait(hub.submit(flow))
        assert run["status"] == "waiting_approval" and not calls
        hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
        run = await hub.wait(run["id"])
        assert run["status"] == "succeeded"
        async with asyncio.timeout(30):
            while (
                not hub.connections.deliveries() or hub.connections.deliveries()[0]["status"] != "delivered"
            ):
                await asyncio.sleep(0.05)
        assert [r[0] for r in calls] == ["old", "old"]
        assert calls[0][1]["idempotency-key"] == calls[1][1]["idempotency-key"]
        assert calls[0][1]["authorization"] == "Bearer secret-value-123"
        rows = (await client.get("/v1/connections/deliveries")).json()
        assert json.loads(rows[0]["receipt"])["etag"] == "receipt-1"
        assert b"secret-value-123" not in Path(hub.store.path).read_bytes()
        restored = Hub(hub.store.path)
        assert restored.connections.secret("testkey") == "secret-value-123"
        assert restored.tools.latest["connection.notice"] == 2
        assert restored.development.credential("testkey") == "secret-value-123"


async def test_calendar_utc_and_cron_dst_recovery(hub):
    remote = FastAPI()
    bodies = []

    @remote.put("/calendar/{filename}")
    async def calendar(filename: str, request: Request):
        bodies.append(await request.body())
        return Response(status_code=201)

    async with live_server(remote) as endpoint:
        hub.connections.save(
            {"id": "calendar", "title": "Calendar", "kind": "caldav", "url": endpoint + "/calendar"}
        )
        r = await hub.wait(
            hub.submit(
                {
                    "name": "event",
                    "steps": [
                        {
                            "id": "event",
                            "target": "connection.calendar",
                            "input": {
                                "title": "repair\nBEGIN:BAD",
                                "start": "2026-09-21T10:00:00+08:00",
                                "end": "2026-09-21T11:00:00+08:00",
                            },
                        }
                    ],
                }
            )
        )
        hub.tools.approve(hub.store, r["approvals"][0]["id"], True)
        await hub.wait(r["id"])
        async with asyncio.timeout(30):
            while not bodies:
                await asyncio.sleep(0.02)
        assert b"DTSTART:20260921T020000Z" in bodies[0]
        assert b"\r\nBEGIN:BAD" not in bodies[0]
    after = datetime(2026, 3, 7, 15, tzinfo=timezone.utc).timestamp()
    first = hub.scheduler.next_cron("0 9 * * *", "America/New_York", after)
    assert datetime.fromtimestamp(first, timezone.utc).hour == 13
    trigger = hub.scheduler.create(
        Trigger(
            name="daily",
            kind="schedule",
            cron="0 9 * * *",
            timezone="America/New_York",
            start_at=after,
            workflow={"name": "daily", "steps": [{"id": "x", "target": "core.echo"}]},
        )
    )
    with hub.store.connect() as db:
        db.execute("UPDATE triggers SET next_at=? WHERE id=?", (time.time() - 3600, trigger["id"]))
    hub.scheduler.tick()
    hub.scheduler.tick()
    with hub.store.connect() as db:
        assert db.execute("SELECT count(*) FROM runs WHERE name='daily'").fetchone()[0] == 1
        assert (
            db.execute("SELECT next_at FROM triggers WHERE id=?", (trigger["id"],)).fetchone()[0]
            > time.time()
        )
