"""Run synthetic task-family demos against a real HTTP service and two SQLite stores."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path

import uvicorn

from easyagent.api import create_app
from easyagent.client import HubClient
from easyagent.contracts import ModelResult, ToolCall
from easyagent.runtime import Hub
from examples.scenarios.world import ScenarioWorld, workflow


async def main():
    with tempfile.TemporaryDirectory(prefix="eah-scenarios-") as directory:
        hub = Hub(Path(directory) / "hub.db", poll_seconds=0.01)
        world = ScenarioWorld(Path(directory) / "business.db")
        world.register(hub)
        class AdversarialFixture:
            async def generate(self, request, model):
                observed = any(m["role"] == "tool" for m in request.messages)
                return ModelResult(tool_calls=[ToolCall(id="attack" if observed else "read",
                    name="scenario.exfiltrate" if observed else "scenario.reviews", arguments={})])
        hub.models.register("adversarial-fixture", AdversarialFixture(), "deliberately-noncompliant", {"chat"})
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(create_app(hub), log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(0.01)
            async with HubClient(f"http://127.0.0.1:{sock.getsockname()[1]}") as client:
                results = []
                for name in ("reminder_known", "reminder_missing", "parallel_quotes", "order_cancel", "untrusted_reviews"):
                    result = await client.wait((await client.submit(workflow(name)))["id"])
                    if result["status"] == "waiting_input":
                        assert not world.lookups or len(world.lookups) == 1
                        await client.respond(result["input_requests"][0]["id"], {"after": "2026-09-19T09:00:00+08:00"})
                        result = await client.wait(result["id"])
                    if result["status"] == "waiting_approval":
                        with world.connect() as db:
                            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
                        await client.approve(result["approvals"][0]["id"])
                        result = await client.wait(result["id"])
                    expected = "failed" if name == "untrusted_reviews" else "succeeded"
                    assert result["status"] == expected, result
                    results.append({"scenario": name, "status": result["status"], "expected": expected})
                with world.connect() as db:
                    assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
                assert world.peak_parallel == 2
                print(json.dumps({"synthetic_task_demos": results, "real_model_score": None}, ensure_ascii=False, indent=2))
        finally:
            server.should_exit = True
            await serving
            sock.close()


if __name__ == "__main__":
    asyncio.run(main())
