"""Configurable duration integration run; a short run is not a production soak test."""
import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from easyagent.runtime import Hub


async def exercise(seconds, path):
    started = time.monotonic()
    count, restarts, latencies = 0, 0, []
    hub = Hub(path, poll_seconds=0.01, lease_seconds=0.3)
    await hub.start()
    try:
        while time.monotonic() - started < seconds:
            cycle = time.monotonic()
            ids = [hub.submit({"name": "endurance", "steps": [
                {"id": "read", "target": "core.echo", "input": {"index": count+i}},
                {"id": "loop", "kind": "foreach", "depends_on": ["read"], "input": {"items": [1, 2]},
                 "body": {"name": "endurance child", "steps": [{"id": "map", "kind": "transform", "input": {"value": {"$ref": "$input.item"}}}]}}
            ]}) for i in range(4)]
            results = await asyncio.gather(*(hub.wait(identifier) for identifier in ids))
            assert all(r["status"] == "succeeded" for r in results), results
            assert all([v["map"]["value"] for v in r["steps"][1]["output"]["results"]] == [1, 2] for r in results)
            count += len(ids)
            # Keep statistics bounded for extended runs.
            latencies.append(time.monotonic()-cycle)
            latencies = latencies[-1000:]
            if count % 40 == 0:
                await hub.stop()
                hub = Hub(path, poll_seconds=0.01, lease_seconds=0.3)
                await hub.start()
                assert hub.store.run(ids[-1])["status"] == "succeeded"
                restarts += 1
            await asyncio.sleep(0.03)
        with hub.store.connect() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("SELECT 1 FROM runs WHERE status!='succeeded' LIMIT 1").fetchone()
            calls = db.execute("SELECT count(*) FROM invocations").fetchone()[0]
            assert calls == count
        print(json.dumps({"seconds": round(time.monotonic()-started, 2), "parent_runs": count,
                          "child_runs": count*2, "clean_restarts": restarts,
                          "last_1000_batch_p95_seconds": round(sorted(latencies)[int((len(latencies)-1)*0.95)], 3),
                          "database_integrity": "ok", "long_soak_claim": False}, indent=2))
    finally:
        await hub.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 604800:
        parser.error("seconds must be between 1 and 604800")
    with tempfile.TemporaryDirectory(prefix="eah-endurance-") as directory:
        asyncio.run(exercise(args.seconds, Path(directory) / "hub.db"))


if __name__ == "__main__":
    main()
