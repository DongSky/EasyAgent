import asyncio
import json
import sys
import time
from pathlib import Path

from easyagent.contracts import ToolSpec
from easyagent.runtime import Hub


async def test_actual_process_kill_restart_recovers_same_invocation(tmp_path):
    database, receipt = tmp_path / "crash.db", tmp_path / "receipt.json"
    hub = Hub(database)
    async def unused(args, ctx):
        return {}
    hub.tools.register(ToolSpec(name="test.idempotent"), unused)
    run_id = hub.submit({"name": "crash restart", "steps": [{"id": "a", "target": "test.idempotent"}]})
    script = Path(__file__).with_name("crash_worker.py")
    first = await asyncio.create_subprocess_exec(sys.executable, str(script), str(database), str(receipt),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    second = None
    try:
        async with asyncio.timeout(10):
            while not receipt.exists():
                await asyncio.sleep(0.02)
        first.kill()
        await first.wait()
        recorded = json.loads(receipt.read_text(encoding='utf-8'))
        second = await asyncio.create_subprocess_exec(sys.executable, str(script), str(database), str(receipt),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        result = await hub.wait(run_id, timeout=10)
        assert result["status"] == "succeeded"
        assert result["steps"][0]["output"] == recorded
        assert result["steps"][0]["attempts"] == 2
        assert json.loads(receipt.read_text(encoding='utf-8'))["writes"] == 1
    finally:
        for process in (first, second):
            if process and process.returncode is None:
                process.kill()
                await process.wait()


async def test_composite_restart_cancel_and_wall_time(tmp_path):
    hub = Hub(tmp_path / "composite.db", lease_seconds=0.2, poll_seconds=0.01)
    body = {"name": "child", "steps": [{"id": "a", "kind": "approval", "input": {"x": {"$ref": "$input.item"}}}]}
    run_id = hub.submit({"name": "nested", "steps": [{"id": "loop", "kind": "foreach", "body": body, "input": {"items": [1, 2]}}]})
    await hub.start()
    await hub.wait(run_id)
    await hub.stop()
    restarted = Hub(tmp_path / "composite.db", lease_seconds=0.2, poll_seconds=0.01)
    await restarted.start()
    try:
        async with asyncio.timeout(5):
            while len(restarted.store.run(run_id)["approvals"]) != 2:
                await asyncio.sleep(0.02)
        restarted.store.cancel(run_id)
        assert all(c["status"] == "cancelled" for c in restarted.store.run(run_id)["children"])
        expired = restarted.submit({"name": "expired", "limits": {"wall_time_seconds": 0.05}, "steps": [
            {"id": "wait", "target": "core.echo", "not_before": time.time()+60}]})
        assert (await restarted.wait(expired))["status"] == "failed"
        limited = restarted.submit({"name": "child cap", "limits": {"child_runs": 1}, "steps": [
            {"id": "loop", "kind": "foreach", "input": {"items": [1, 2]}, "body": body}]})
        result = await restarted.wait(limited)
        assert result["status"] == "failed"
    finally:
        await restarted.stop()


async def test_two_hubs_compete_without_duplicate_success(tmp_path):
    path = tmp_path / "shared.db"
    # Exercise normal claim contention, not lease expiry under a busy test host.
    # Expired idempotent calls may legitimately retry; crash recovery is tested above.
    hubs = [Hub(path, poll_seconds=0.01, lease_seconds=5) for _ in range(2)]
    seen = []
    async def effect(args, ctx):
        seen.append(ctx.invocation_id)
        await asyncio.sleep(0.005)
        return {"value": args["value"]}
    for hub in hubs:
        hub.tools.register(ToolSpec(name="test.effect"), effect)
        await hub.start()
    try:
        runs = [hubs[0].submit({"name": f"burst-{i}", "steps": [{"id": "a", "target": "test.effect", "input": {"value": i}}]}) for i in range(30)]
        results = await asyncio.gather(*(hubs[0].wait(r) for r in runs))
        assert all(r["status"] == "succeeded" for r in results)
        assert len(seen) == len(set(seen)) == 30
    finally:
        for hub in hubs:
            await hub.stop()
