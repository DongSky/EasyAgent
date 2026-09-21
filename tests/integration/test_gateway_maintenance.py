import asyncio
import hashlib
import hmac
import json
import time

import httpx
from conftest import live_server
from easyagent.api import create_app
from easyagent.contracts import ModelResult
from test_conversations import settle


class ReviewModel:
    async def generate(self, request, model):
        if request.response_schema:
            return ModelResult(
                data={
                    "instructions": "Confirm source evidence and verify the final receipt before closing the task."
                },
                usage={"mock": True},
            )
        return ModelResult(text="A compact summary of the original messages.", usage={"mock": True})


async def test_signed_inbound_dedup_conversation_reply_approval_and_automatic_learning(hub):
    hub.connections.put_secret("gatewaykey", "integration-channel-secret")
    hub.connections.save(
        {"id": "reply", "title": "Channel reply", "kind": "webhook", "url": "http://127.0.0.1:9/unused"}
    )
    hub.gateway.save({"id": "chat", "credential": "gatewaykey", "model": "mock", "reply_connector": "reply"})
    app = create_app(hub, token="operator-secret", manage_workers=False)
    async with live_server(app) as url, httpx.AsyncClient(base_url=url, timeout=30) as client:
        raw = json.dumps(
            {"event_id": "message-1", "thread": "private-thread", "text": "remember boxes"}
        ).encode()
        timestamp = str(int(time.time()))
        headers = {
            "x-eah-timestamp": timestamp,
            "x-eah-signature": hmac.new(
                b"integration-channel-secret", timestamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest(),
            "content-type": "application/json",
        }
        response = await client.post("/v1/gateway/incoming/chat", content=raw, headers=headers)
        assert response.status_code == 202, response.text
        duplicate = await client.post("/v1/gateway/incoming/chat", content=raw, headers=headers)
        assert duplicate.json()["duplicate"]
        assert (
            await client.post(
                "/v1/gateway/incoming/chat", content=raw, headers={**headers, "x-eah-signature": "wrong"}
            )
        ).status_code == 403
        c = await settle(hub, response.json()["conversation"])
        assert len(c["messages"]) == 2
        hub.gateway.replies()
        with hub.store.connect() as db:
            run_id = db.execute("SELECT delivery_run FROM gateway_replies").fetchone()[0]
        assert (await hub.wait(run_id))["status"] == "waiting_approval"
        assert not hub.connections.deliveries()
    hub.models.register("reviewer", ReviewModel(), "fixture", ["chat", "decision"])
    hub.maintenance.configure(
        {
            "auto_compact": True,
            "compact_after_chars": 4000,
            "review_model": "reviewer",
            "review_limit_per_day": 1,
        }
    )
    r = await hub.wait(
        hub.submit(
            {
                "name": "learnable",
                "metadata": {"learn_as": "receipts"},
                "steps": [{"id": "x", "target": "core.echo"}],
            }
        )
    )
    await hub.maintenance.tick()
    skills = hub.learning.catalog()
    assert len(skills) == 1 and skills[0]["source_run"] == r["id"] and skills[0]["status"] == "candidate"
    await hub.maintenance.tick()
    assert len(hub.learning.catalog()) == 1
    c = await hub.conversations.create({"title": "long conversation", "model": "reviewer"})
    for i in range(4):
        await hub.conversations.send(c["id"], {"text": str(i) + "x" * 1600})
        await settle(hub, c["id"])
    # The background loop may already own this compaction. A second tick skips
    # claimed work; returning from it does not mean the summary has been saved.
    async with asyncio.timeout(30):
        while not (c := hub.conversations.get(c["id"]))["summary"]:
            await hub.maintenance.tick()
            await asyncio.sleep(.01)
    assert c["summary"] and len(c["messages"]) == 8


async def test_memory_merge_conflict_provenance_and_granted_agent_boundary(hub):
    import pytest
    from easyagent.components import digest
    from easyagent.store import Conflict

    hub.store.memory_put("family", "one", {"date": "Monday"}, "school")
    hub.store.memory_put("family", "two", {"owner": "Lee"}, "parent")
    body = {
        "destination": "plan",
        "sources": {"one": digest({"date": "Monday"}), "two": digest({"owner": "Lee"})},
        "value": {"date": "Monday", "owner": "Lee"},
    }
    merged = hub.memory_management.merge("family", body)
    assert len(merged["archive_ids"]) == 2
    assert hub.store.memory_search("family")[0]["key"] == "plan"
    with pytest.raises(Conflict):
        hub.memory_management.merge("family", body)
    r = await hub.wait(
        hub.submit(
            {
                "name": "scope",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "mock",
                        "input": {
                            "tools": ["memory.search"],
                            "prompt": json.dumps(
                                {"tool": "memory.search", "arguments": {"namespace": "family"}}
                            ),
                        },
                    }
                ],
            }
        )
    )
    # The ungranted namespace is refused; the refusal reaches the model as an observation, never as data.
    assert r["status"] == "succeeded"
    observed = [m for m in r["steps"][0]["state"]["messages"] if m["role"] == "tool"]
    assert len(observed) == 1 and "namespace not granted" in observed[0]["content"] and "Monday" not in observed[0]["content"]
    assert "tool.failed_observed" in {e["kind"] for e in hub.store.events(r["id"])}
