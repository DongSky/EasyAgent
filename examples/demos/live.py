"""Explicit opt-in provider smoke run. Requires a deployer-authored config and credentials."""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from easyagent.config import configure
from easyagent.runtime import Hub


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", default="Reply with a short hello.")
    parser.add_argument("--capability", default="chat")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="eah-live-") as directory:
        hub = Hub(Path(directory) / "live.db")
        await configure(hub, args.config)
        await hub.start()
        try:
            run = await hub.wait(hub.submit({"name": "explicit live smoke", "steps": [{"id": "model", "kind": "model", "target": args.model,
                "input": {"capability": args.capability, "prompt": args.prompt}, "max_attempts": 1}]}), timeout=120)
            print(json.dumps(run, ensure_ascii=False, indent=2))
            if run["status"] != "succeeded":
                raise SystemExit(1)
        finally:
            await hub.stop()


if __name__ == "__main__":
    asyncio.run(main())
