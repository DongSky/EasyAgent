# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

"""Small Python extension entrypoint. Protocol also works from JS and Rust."""

import inspect
import json
import sys
import asyncio


class Extension:
    def __init__(self):
        self.handlers = {}

    def handler(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn

        return register

    def run(self, *, persistent=False):
        async def dispatch(request):
            if request.get("protocol_version") != 1:
                raise ValueError("unsupported protocol")
            if request["method"].startswith("lifecycle.") and request["method"] not in self.handlers:
                return {"result": {}}
            handler = self.handlers[request["method"]]
            result = handler(request["params"], request["context"])
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, ExtensionResponse) else {"result": result}

        async def serve():
            while line := sys.stdin.buffer.readline(2_000_001):
                if len(line) > 2_000_000:
                    raise ValueError("request exceeds 2 MB")
                print(json.dumps(await dispatch(json.loads(line)), ensure_ascii=False), flush=True)
                if not persistent:
                    break

        asyncio.run(serve())


class ExtensionResponse(dict):
    """Use for state updates or typed service continuations."""
