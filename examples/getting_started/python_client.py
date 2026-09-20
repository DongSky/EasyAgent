# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

"""Run from the repository root against a running Hub; no model key needed."""

import asyncio
import json
import os
from pathlib import Path

from easyagent_client import HubClient


async def main():
    workflow = json.loads(Path(__file__).parents[1].joinpath("first-workflow.json").read_text(encoding="utf-8"))
    async with HubClient(
        os.environ.get("EAH_URL", "http://127.0.0.1:8765"),
        os.environ.get("EAH_TOKEN", ""),
    ) as client:
        created = await client.submit(workflow)
        run = await client.wait(created["id"])
        print(json.dumps(run, ensure_ascii=False, indent=2))
        if run["status"] != "succeeded":
            raise SystemExit("Inspect the run status, approvals and input_requests before continuing.")


if __name__ == "__main__":
    asyncio.run(main())
