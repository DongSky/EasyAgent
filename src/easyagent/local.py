"""Synchronous and asynchronous SDK sessions using the existing durable Hub."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path

from easyagent_client.workflows import RunStopped

from .contracts import Workflow
from .modules import Module
from .results import run_result
from .runtime import Hub

active_runtime = ContextVar("easyagent_runtime", default=None)


@dataclass
class Result:
    state: dict

    @property
    def id(self):
        return self.state["id"]

    @property
    def outputs(self):
        return self.state["outputs"]

    @property
    def value(self):
        return self.outputs.get("result", self.outputs)

    @property
    def artifacts(self):
        return self.state["artifacts"]


def require_sync():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("An event loop is running; use 'async with Runtime()' and 'await runtime.arun(...)' or 'await module.acall(...)'")


class Runtime:
    """A local SDK session, without an HTTP server or browser.

    Calls share one Hub and SQLite database. Register code before resuming runs.
    Hub remains public for advanced providers, extensions, policies and scheduling.
    """
    def __init__(self, database=None, *, config=None, timeout=600, key=None, hub=None):
        self.hub = hub or Hub(database or os.environ.get("EAH_DATABASE", ".eah/local.db"), run_roots=set())
        self.config, self.timeout, self.key = config, timeout, key
        self._runner, self._entered, self._token = None, False, None
        self.functions = {}

    async def _start(self):
        from .config import configure
        await configure(self.hub, self.config)
        # Workers are started on execution, after custom tool registration.
        self._entered = True

    def __enter__(self):
        require_sync()
        if self._entered:
            raise RuntimeError("Runtime cannot be entered twice")
        self._runner = asyncio.Runner()
        try:
            self._runner.run(self._start())
        except BaseException:
            self._runner.close()
            raise
        self._token = active_runtime.set(self)
        return self

    def __exit__(self, *exc):
        try:
            self._runner.run(self.hub.stop())
        finally:
            active_runtime.reset(self._token)
            self._runner.close()
            self._runner, self._entered = None, False

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError("Runtime cannot be entered twice")
        await self._start()
        self._token = active_runtime.set(self)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.hub.stop()
        finally:
            active_runtime.reset(self._token)
            self._entered = False

    def bind(self, module):
        flow, trace = module.build()
        for name, function in trace.functions.items():
            if name in self.functions:
                if self.functions[name] != function.fingerprint:
                    raise ValueError("a different tool is already bound: " + name)
            else:
                self.hub.tools.register(function.spec, function.handler)
                self.functions[name] = function.fingerprint
        for name, capabilities in trace.models.items():
            if name not in self.hub.models.bindings:
                from .models import HTTPProvider
                key = os.environ.get("OPENAI_API_KEY", "")
                if not key:
                    raise ValueError(f"Model {name!r} is not registered; configure a provider or set OPENAI_API_KEY")
                self.hub.models.register(name, HTTPProvider(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), key),
                                         name, capabilities)
        return flow

    def _check_functions(self, flow):
        for name, fingerprint in flow.metadata.get("sdk_functions", {}).items():
            if self.functions.get(name) != fingerprint:
                raise ValueError("Register the original @tool code before executing/resuming: " + name)

    async def arun(self, module: Module | Workflow | dict, *args, **inputs):
        if not self._entered:
            raise RuntimeError("Use Runtime as a context manager")
        if isinstance(module, Module):
            bound = module.input_signature.bind(*args, **inputs)
            bound.apply_defaults()
            flow = self.bind(module)
            flow.inputs.update(bound.arguments)
        else:
            if args:
                raise TypeError("Workflow inputs must be named")
            flow = Workflow.model_validate(module).model_copy(deep=True)
            flow.inputs.update(inputs)
        self._check_functions(flow)
        if schema := flow.metadata.get("input_schema"):
            from jsonschema import validate
            validate(flow.inputs, schema)
        run_id = self.hub.submit(flow, self.key)
        return await self.aresume(run_id)

    async def aresume(self, run_id):
        if not self._entered:
            raise RuntimeError("Use Runtime as a context manager")
        self._check_functions(Workflow.model_validate(self.hub.store.run(run_id)["spec"]))
        if self.hub.run_roots is not None:
            self.hub.run_roots.add(run_id)
        await self.hub.start()
        try:
            await self.hub.wait(run_id, timeout=self.timeout)
        except TimeoutError:
            error = TimeoutError(f"Run {run_id} timed out locally; resume the same run with its database and code")
            error.run_id = run_id
            raise error from None
        state = run_result(self.hub, run_id)
        if state["status"] != "succeeded":
            raise RunStopped(state)
        return Result(state)

    def run(self, module, *args, **inputs):
        require_sync()
        if self._runner is None:
            raise RuntimeError("Use 'with Runtime()' for synchronous calls")
        return self._runner.run(self.arun(module, *args, **inputs))

    def resume(self, run_id):
        require_sync()
        if self._runner is None:
            raise RuntimeError("Use 'with Runtime()' for synchronous calls")
        return self._runner.run(self.aresume(run_id))

    def upload(self, path):
        import mimetypes
        path = Path(path)
        with path.open("rb") as file:
            data = file.read(50_000_001)
        if len(data) > 50_000_000:
            raise ValueError("upload exceeds 50 MB")
        return self.hub.artifacts.put(path.name, data, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
