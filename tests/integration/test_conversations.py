import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from conftest import live_server
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub


async def settle(hub, identifier):
    async with asyncio.timeout(30):
        while True:
            await hub.conversations.tick()
            c = hub.conversations.get(identifier)
            if not c["active_run"] and all(
                t["status"] not in ("queued", "starting", "running") for t in c["turns"]
            ):
                return c
            await asyncio.sleep(0.02)


async def test_conversation_queue_steer_stream_fork_compact_restart(api, tmp_path):
    url, hub = api
    remote = FastAPI()
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    @remote.post("/chat/completions")
    async def model(request: Request):
        body = await request.json()
        seen.append(body)
        if len(seen) == 1:
            started.set()
            await release.wait()
        text = ";".join(m["content"] for m in body["messages"] if m["role"] == "user")[-100:]
        if not body.get("stream"):
            return {"choices": [{"message": {"content": "summary retains original evidence"}}]}

        async def events():
            for chunk in [text[: len(text) // 2], text[len(text) // 2 :]]:
                yield "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n\n"
                await asyncio.sleep(0.01)
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"completion_tokens": 8, "prompt_tokens": 5},
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        hub.models.register("session-model", HTTPProvider(endpoint, ""), "fixture", ["chat"])
        c = (
            await client.post(
                "/v1/conversations",
                json={
                    "title": "持续任务",
                    "model": "session-model",
                    "agent": {"prompt": "", "streaming": True},
                },
            )
        ).json()
        first = await client.post(
            f"/v1/conversations/{c['id']}/messages",
            json={"text": "remember apples", "idempotency_key": "first"},
        )
        assert first.status_code == 202, first.text
        await asyncio.wait_for(started.wait(), 30)
        await client.post(
            f"/v1/conversations/{c['id']}/messages", json={"text": "change to pears", "mode": "steer"}
        )
        await client.post(f"/v1/conversations/{c['id']}/messages", json={"text": "what did I choose?"})
        release.set()
        result = await settle(hub, c["id"])
        assert len(result["messages"]) == 5
        assert "pears" in result["messages"][-1]["content"]
        run = hub.store.run(first.json()["run_id"])
        assert any(e["kind"] == "model.delta" for e in hub.store.events(run["id"]))
        assert any(e["kind"] == "session.steered" for e in hub.store.events(run["id"]))
        assert hub.conversations.search("pears", c["id"])
        compact = await client.post(f"/v1/conversations/{c['id']}/compact", json={"keep_last": 2})
        assert compact.status_code == 200 and compact.json()["original_messages_retained"]
        branch = await client.post(
            f"/v1/conversations/{c['id']}/fork", json={"message_id": result["messages"][0]["id"]}
        )
        assert branch.status_code == 201 and len(branch.json()["messages"]) == 1
        restored = Hub(hub.store.path, poll_seconds=0.01)
        assert restored.conversations.get(c["id"])["summary"]
        assert len(restored.conversations.get(c["id"])["messages"]) == 5


async def test_streaming_tool_arguments_and_truncated_stream_fail(hub):
    remote = FastAPI()

    @remote.post("/chat/completions")
    async def model(request: Request):
        body = await request.json()

        async def stream():
            if body["model"] == "broken":
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                return
            if any(m["role"] == "tool" for m in body["messages"]):
                yield 'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            else:
                chunks = [
                    {"index": 0, "id": "call1", "function": {"name": "tool_0", "arguments": '{"text":'}},
                    {"index": 0, "function": {"arguments": '"echo"}'}},
                ]
                for chunk in chunks:
                    yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [chunk]}}]}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async with live_server(remote) as endpoint:
        hub.models.register("stream", HTTPProvider(endpoint, ""), "fixture", ["chat"])
        hub.models.register("broken", HTTPProvider(endpoint, ""), "broken", ["chat"])
        flow = {
            "name": "streaming tools",
            "steps": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "target": "stream",
                    "input": {"prompt": "call echo", "tools": ["core.echo"], "streaming": True},
                }
            ],
        }
        run = await hub.wait(hub.submit(flow))
        assert run["status"] == "succeeded" and run["steps"][0]["output"]["tool_count"] == 1
        flow["steps"][0]["target"] = "broken"
        run = await hub.wait(hub.submit(flow))
        assert run["status"] == "failed" and "terminal event" in run["steps"][0]["error"]
