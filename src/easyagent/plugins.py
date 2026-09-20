from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from pydantic import Field

from .contracts import Contract, ToolSpec


class PluginManifest(Contract):
    api_version: str = "1"
    name: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    tools: list[ToolSpec] = Field(min_length=1)
    env_allow: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    isolation: str = "process"
    image: str | None = None


async def bounded_read(stream, limit):
    data = bytearray()
    while chunk := await stream.read(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("plugin output exceeds byte limit")
    return bytes(data)


def load_plugin(registry, path):
    path = Path(path).resolve()
    manifest = PluginManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.api_version != "1":
        raise ValueError("unsupported plugin protocol version")
    if manifest.isolation != "process" or manifest.image:
        raise ValueError(
            "legacy plugins support trusted process only; use JS/WASM extensions for controlled computation"
        )
    env_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"} | set(manifest.env_allow)
    env = {k: v for k, v in os.environ.items() if k in env_keys}
    for spec in manifest.tools:

        def make_handler(tool):
            async def call(arguments, context):
                command = manifest.command
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=path.parent,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                tasks = []
                try:
                    payload = {
                        "protocol_version": "1",
                        "id": context.invocation_id,
                        "method": "tools/call",
                        "params": {"name": tool.name, "arguments": arguments},
                        "context": {"invocation_id": context.invocation_id},
                    }
                    async with asyncio.timeout(manifest.timeout_seconds):
                        tasks = [
                            asyncio.create_task(bounded_read(process.stdout, manifest.max_output_bytes)),
                            asyncio.create_task(bounded_read(process.stderr, 65536)),
                        ]
                        process.stdin.write((json.dumps(payload) + "\n").encode())
                        await process.stdin.drain()
                        process.stdin.close()
                        output, _ = await asyncio.gather(*tasks)
                        await process.wait()
                    if process.returncode:
                        raise RuntimeError(f"plugin {manifest.name} exited with {process.returncode}")
                    data = json.loads(output)
                    if data.get("id") != context.invocation_id:
                        raise ValueError("plugin response id mismatch")
                    if "error" in data:
                        raise RuntimeError(f"plugin {manifest.name} returned an error")
                    return data["result"]
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

            return call

        registry.register(spec, make_handler(spec))
    return manifest
