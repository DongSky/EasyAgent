# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0
"""Small convenience layer over the public workflow API; no server dependency."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import mimetypes
from pathlib import Path
import uuid
from urllib.parse import quote, urlparse

import httpx

MAX_FILE_BYTES = 50_000_000


class RunStopped(RuntimeError):
    def __init__(self, state):
        self.state, self.run_id, self.status = state, state['id'], state['status']
        super().__init__(f"Run {self.run_id}: {self.status}. Inspect state, supply approval/input, then resume this run ID.")


@dataclass
class RunResult:
    client: object
    state: dict

    @property
    def id(self):
        return self.state['id']

    @property
    def outputs(self):
        return self.state['outputs']

    @property
    def artifacts(self):
        return self.state['artifacts']

    async def download(self, output, destination):
        """Explicitly download a named artifact or HTTPS media URL, atomically."""
        value = self.outputs[output]
        if isinstance(value, dict) and value.get('id') and value.get('digest'):
            return await download(self.client.http, '/v1/artifacts/' + quote(value['id'], safe='') + '/content', destination)
        if isinstance(value, str):
            parsed = urlparse(value)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                    or parsed.fragment or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
                raise ValueError('media URL must use HTTPS (or loopback HTTP), without embedded credentials')
            # Remote result URLs never receive the Hub bearer token.
            async with httpx.AsyncClient(timeout=120, follow_redirects=False) as anonymous:
                return await download(anonymous, value, destination)
        raise ValueError('named output must be an artifact descriptor or a media URL')


@dataclass
class RunHandle:
    client: object
    id: str

    async def result(self, timeout=600, *, poll_interval=1):
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError('timeout and poll_interval must be positive')
        try:
            async with asyncio.timeout(timeout):
                while True:
                    state = await self.client.request('GET', '/v1/runs/' + quote(self.id, safe='') + '/result')
                    if state['status'] == 'succeeded':
                        return RunResult(self.client, state)
                    if state['status'] in ('failed', 'cancelled', 'waiting_approval', 'waiting_input', 'needs_attention'):
                        raise RunStopped(state)
                    await asyncio.sleep(poll_interval)
        except TimeoutError:
            raise TimeoutError(f'Run {self.id} is still available; resume it instead of resubmitting. The run was not cancelled.') from None

    async def cancel(self):
        return await self.client.cancel(self.id)


@dataclass
class WorkflowHandle:
    client: object
    id: str
    revision: int | None = None

    async def run(self, inputs=None, *, key=None, timeout=600):
        from .sync import prepare_inputs
        job = await self.start(await prepare_inputs(self.client, inputs or {}), key=key)
        return await job.result(timeout=timeout)

    async def start(self, inputs=None, *, key=None):
        created = await self.client.request('POST', '/v1/workflows/' + quote(self.id, safe='') + '/runs',
                                            {'inputs': inputs or {}, 'revision': self.revision}, idempotency_key=key)
        return RunHandle(self.client, created['id'])

    async def definition(self):
        suffix = '?revision=' + str(self.revision) if self.revision is not None else ''
        return await self.client.request('GET', '/v1/workflows/' + quote(self.id, safe='') + suffix)


async def upload_file(client, filename):
    path = Path(filename)
    with path.open('rb') as file:
        content = file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('file exceeds 50 MB upload limit')
    response = await client.http.post('/v1/artifacts/upload', params={'name': path.name}, content=content,
                                      headers={'Content-Type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'})
    response.raise_for_status()
    return response.json()


async def download(http, url, filename):
    destination = Path(filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
    try:
        async with http.stream('GET', url, follow_redirects=False) as response:
            response.raise_for_status()
            total = 0
            with temporary.open('xb') as file:
                async for block in response.aiter_bytes():
                    total += len(block)
                    if total > MAX_FILE_BYTES:
                        raise ValueError('download exceeds 50 MB limit')
                    file.write(block)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
