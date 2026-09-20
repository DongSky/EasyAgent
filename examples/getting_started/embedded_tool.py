# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

"""Embed the Hub and register a Python tool, with a separate demo database."""

import argparse
import asyncio
import json

from easyagent.contracts import ToolSpec
from easyagent.runtime import Hub


async def main(database):
    hub = Hub(database)

    async def add(arguments, context):
        return {"value": arguments["a"] + arguments["b"]}

    hub.tools.register(
        ToolSpec(
            name="math.add",
            description="Add two numbers without external side effects",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"], "additionalProperties": False,
            },
            output_schema={
                "type": "object", "properties": {"value": {"type": "number"}},
                "required": ["value"], "additionalProperties": False,
            },
            effect="read", idempotent=True,
        ),
        add,
    )
    await hub.start()
    try:
        run = await hub.wait(hub.submit({
            "name": "Embedded Python tool",
            "steps": [{"id": "sum", "target": "math.add", "input": {"a": 2, "b": 3}}],
        }))
        if run["status"] != "succeeded" or run["steps"][0]["output"] != {"value": 5}:
            raise RuntimeError("Unexpected run result: " + json.dumps(run))
        print(json.dumps({"status": run["status"], "output": run["steps"][0]["output"]}))
    finally:
        await hub.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=".eah/getting-started-embedded.db")
    asyncio.run(main(parser.parse_args().database))
