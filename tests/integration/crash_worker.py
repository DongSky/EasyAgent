"""Separate process used by the kill/restart integration scenario."""
import asyncio
import json
import sys
from pathlib import Path

from easyagent.contracts import ToolSpec
from easyagent.runtime import Hub


async def main():
    database, receipt_path = sys.argv[1:]
    receipt = Path(receipt_path)
    hub = Hub(database, lease_seconds=0.3, poll_seconds=0.01)

    async def durable_effect(args, context):
        if receipt.exists():
            return json.loads(receipt.read_text(encoding='utf-8'))
        value = {"invocation_id": context.invocation_id, "writes": 1}
        receipt.write_text(json.dumps(value), encoding='utf-8')
        await asyncio.sleep(30)
        return value

    hub.tools.register(ToolSpec(name="test.idempotent", idempotent=True), durable_effect)
    await hub.start()
    await asyncio.Event().wait()


asyncio.run(main())
