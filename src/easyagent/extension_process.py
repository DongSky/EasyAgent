"""Persistent trusted NDJSON workers. Cancellation/timeout terminates the process tree."""

import asyncio
import json
import os
import signal
from .plugins import bounded_read
from .store import encode


async def terminate(process):
    if process.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
                if process.returncode is None:
                    process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


class PersistentWorker:
    def __init__(self, command, directory, env):
        self.command, self.directory, self.env = command, directory, env
        self.process = None
        self.stderr = None
        self.lock = asyncio.Lock()

    async def call(self, payload, timeout):
        async with self.lock:
            started = False
            if self.process is None or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=self.directory,
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1_100_000,
                    start_new_session=os.name == "posix",
                )
                self.stderr = asyncio.create_task(bounded_read(self.process.stderr, 32000))
                started = True
            try:
                async with asyncio.timeout(timeout):
                    if started and not payload["method"].startswith("lifecycle."):
                        activate = {**payload, "method": "lifecycle.activate", "params": {"restored": True}}
                        self.process.stdin.write((encode(activate) + "\n").encode())
                        await self.process.stdin.drain()
                        initialized = json.loads(await self.process.stdout.readline())
                        if set(initialized) != {"result"}:
                            raise ValueError("activation must return result")
                    self.process.stdin.write((encode(payload) + "\n").encode())
                    await self.process.stdin.drain()
                    output = await self.process.stdout.readline()
                if len(output) > 1_000_000:
                    raise ValueError("extension output too large")
                return json.loads(output)
            except BaseException:
                await self.close()
                raise

    async def close(self):
        if self.process:
            await terminate(self.process)
        if self.stderr:
            self.stderr.cancel()
            await asyncio.gather(self.stderr, return_exceptions=True)
        self.process = None
