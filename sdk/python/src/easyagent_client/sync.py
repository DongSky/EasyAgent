# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0
"""Synchronous remote client for scripts and terminals; no server imports."""
import asyncio
import os
from pathlib import Path

from .client import HubClient


class Client:
    def __init__(self, url=None, token=None):
        self.url = url or os.environ.get("EAH_URL", "http://127.0.0.1:8765")
        self.token = os.environ.get("EAH_TOKEN", "") if token is None else token
        self._runner = None

    def __enter__(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Use async HubClient inside a running event loop")
        if self._runner is not None:
            raise RuntimeError("Client cannot be entered twice")
        self._runner = asyncio.Runner()
        self.async_client = HubClient(self.url, self.token)
        return self

    def __exit__(self, *exc):
        try:
            self._runner.run(self.async_client.http.aclose())
        finally:
            self._runner.close()
            self._runner = None

    def _require_open(self):
        if self._runner is None:
            raise RuntimeError("Use 'with Client() as client'")

    def workflow(self, identifier, *, revision=None, key=None, timeout=600):
        self._require_open()
        return CallableWorkflow(self, identifier, revision, key, timeout)

    def resume(self, run_id, *, timeout=600):
        self._require_open()
        return Result(self, self._runner.run(self.async_client.run_handle(run_id).result(timeout)))


class CallableWorkflow:
    def __init__(self, client, identifier, revision, key, timeout):
        self.client, self.identifier, self.revision = client, identifier, revision
        self.key, self.timeout = key, timeout

    def __call__(self, **inputs):
        self.client._require_open()
        result = self.client._runner.run(self.client.async_client.workflow(self.identifier, revision=self.revision).run(
            inputs, key=self.key, timeout=self.timeout))
        return Result(self.client, result)


class Result:
    def __init__(self, client, result):
        self.client, self._result = client, result
        self.id, self.outputs, self.artifacts, self.state = result.id, result.outputs, result.artifacts, result.state

    def download(self, name, destination):
        self.client._require_open()
        return self.client._runner.run(self._result.download(name, destination))


async def prepare_inputs(client, value):
    if isinstance(value, Path):
        return (await client.upload_file(value))["id"]
    if isinstance(value, dict):
        return {k: await prepare_inputs(client, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [await prepare_inputs(client, v) for v in value]
    return value
