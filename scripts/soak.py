"""Executable long-stability harness. Actual elapsed time is always included in results."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from easyagent.runtime import Hub


async def main(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    hub = Hub(root / "hub.db", poll_seconds=0.02)
    await hub.start()
    started = time.monotonic()
    runs = 0
    failures = []
    restarts = 0
    try:
        while time.monotonic() - started < args.seconds:
            flow = {
                "name": "soak",
                "steps": [
                    {"id": "a", "target": "core.echo", "input": {"index": runs}},
                    {
                        "id": "b",
                        "kind": "transform",
                        "depends_on": ["a"],
                        "input": {"value": {"$ref": "a.index"}},
                    },
                ],
            }
            key = f"soak:{started}:{runs}"
            identifier = hub.submit(flow, key)
            assert hub.submit(flow, key) == identifier
            r = await hub.wait(identifier)
            if r["status"] != "succeeded" or r["steps"][1]["output"] != {"value":runs}:
                failures.append({"run": identifier, "status": r["status"]})
            runs += 1
            if runs % args.restart_every == 0:
                await hub.stop()
                hub = Hub(root / "hub.db", poll_seconds=0.02)
                await hub.start()
                restarts += 1
            await asyncio.sleep(0.05)
    finally:
        await hub.stop()
    elapsed = time.monotonic() - started
    report = {
        "requested_seconds": args.seconds,
        "elapsed_seconds": elapsed,
        "runs": runs,
        "restarts": restarts,
        "failures": failures,
        "completed": elapsed >= args.seconds,
        "is_72_hour_evidence": elapsed >= 72 * 3600,
    }
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({**report,"failures":report["failures"][:5],"failure_count":len(report["failures"])}))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=72 * 3600)
    parser.add_argument("--restart-every", type=int, default=100)
    parser.add_argument("--output", default=".eah/soak")
    asyncio.run(main(parser.parse_args()))
