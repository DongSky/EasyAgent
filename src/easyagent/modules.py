"""Callable modules compile to the public durable Workflow contract."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import inspect
import marshal
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import ConfigDict, TypeAdapter, create_model

from .contracts import AgentConfig, Step, ToolSpec, Workflow

_trace = ContextVar("easyagent_trace", default=None)
_executing_function = ContextVar("easyagent_executing_function", default=False)


class Ref:
    """Symbolic workflow value. Indexing selects a field without executing it."""

    def __init__(self, path):
        self.path = path

    def __getitem__(self, key):
        if not isinstance(key, (str, int)) or not str(key) or "." in str(key):
            raise ValueError("reference keys must be non-empty strings without dots or integer indexes")
        return Ref(f"{self.path}.{key}")

    def __bool__(self):
        raise TypeError("A workflow value is unknown while compiling; use Workflow.when or a tool for dynamic logic")

    def __str__(self):
        raise TypeError("Do not format symbolic values into strings; use a @tool to format them at runtime")

    def __iter__(self):
        raise TypeError("Cannot iterate a symbolic value; use a tool or Workflow.foreach")

    def __eq__(self, other):
        raise TypeError("Cannot compare a symbolic value; use Workflow.when or a tool")


def encode_value(value):
    if isinstance(value, Ref):
        return {"$ref": value.path}
    if isinstance(value, dict):
        return {k: encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    return value


def signature(function):
    sig = inspect.signature(function)
    if any(p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) for p in sig.parameters.values()):
        raise TypeError("Use named parameters (no positional-only parameters, *args or **kwargs)")
    return sig


class Trace:
    def __init__(self):
        self.steps, self.functions, self.models = [], {}, {}

    def node(self, kind, target, arguments, **options):
        data = encode_value(arguments)

        def dependencies(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    head = value["$ref"].split(".")[0]
                    return {head} if head != "$input" else set()
                return set().union(*(dependencies(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(dependencies(v) for v in value))
            return set()

        identifier = f"step_{len(self.steps) + 1}"
        literal_fields = {"response_schema", "development"} if kind in ("agent", "model") else set()
        dependency_input = {k: v for k, v in data.items() if k not in literal_fields}
        self.steps.append(Step(id=identifier, kind=kind, target=target, input=data,
                               depends_on=sorted(dependencies(dependency_input)), **options))
        return Ref(identifier)

    def register(self, function):
        old = self.functions.get(function.spec.name)
        if old and old.fingerprint != function.fingerprint:
            raise ValueError("different functions use the same tool name: " + function.spec.name)
        self.functions[function.spec.name] = function


class Module:
    """Subclass forward() to compose modules, just like ordinary Python calls."""

    def forward(self, *args, **kwargs):
        raise NotImplementedError

    def __call__(self, *args, **kwargs):
        if _executing_function.get():
            raise RuntimeError("Inside a node/tool, call ordinary Python helpers; compose SDK modules in Module.forward or Subflow")
        if _trace.get() is not None:
            return self.forward(*args, **kwargs)
        from .local import Runtime, active_runtime
        if runtime := active_runtime.get():
            return runtime.run(self, *args, **kwargs).value
        with Runtime() as runtime:
            return runtime.run(self, *args, **kwargs).value

    async def acall(self, *args, **kwargs):
        if _executing_function.get():
            raise RuntimeError("Inside a node/tool, call ordinary Python helpers; compose SDK modules in Module.forward or Subflow")
        from .local import Runtime, active_runtime
        if runtime := active_runtime.get():
            return (await runtime.arun(self, *args, **kwargs)).value
        async with Runtime() as runtime:
            return (await runtime.arun(self, *args, **kwargs)).value

    @property
    def input_signature(self):
        return signature(self.forward)

    @property
    def input_schema(self):
        hints = get_type_hints(self.forward)
        fields = {name: (hints.get(name, Any), ... if p.default is p.empty else p.default)
                  for name, p in self.input_signature.parameters.items()}
        return create_model(type(self).__name__ + "Input", __config__=ConfigDict(extra="forbid"), **fields).model_json_schema()

    def build(self):
        trace = Trace()
        sig = self.input_signature
        placeholders = {name: Ref("$input." + name) for name in sig.parameters}
        token = _trace.set(trace)
        try:
            output = self(**placeholders)
        finally:
            _trace.reset(token)
        if not trace.steps:
            output = trace.node("tool", "core.echo", {"value": output})["value"]
        defaults = {name: p.default for name, p in sig.parameters.items() if p.default is not p.empty}
        flow = Workflow(name=type(self).__name__, steps=trace.steps, inputs=defaults, metadata={
            "outputs": {"result": encode_value(output)},
            "sdk_functions": {name: f.fingerprint for name, f in trace.functions.items()},
            "input_schema": self.input_schema,
        })
        # Verify serializability now, before submitting any work.
        flow.model_dump_json()
        return flow, trace

    def workflow(self):
        return self.build()[0]

    def export(self, path):
        target = Path(path)
        target.write_text(self.workflow().model_dump_json(indent=2), encoding="utf-8")
        return target


class Sequential(Module):
    """A single-value chain. Use Module.forward for multiple inputs or branches."""

    def __init__(self, *modules):
        if not modules or any(not isinstance(m, Module) for m in modules):
            raise TypeError("Sequential requires one or more Modules")
        self.modules = modules

    @property
    def input_signature(self):
        return self.modules[0].input_signature

    @property
    def input_schema(self):
        return self.modules[0].input_schema

    def forward(self, *args, **inputs):
        value = self.modules[0](*args, **inputs)
        for module in self.modules[1:]:
            try:
                module.input_signature.bind(value)
            except TypeError as exc:
                raise TypeError("Sequential passes one result to the next module; use Module.forward to connect multiple inputs") from exc
            value = module(value)
        return value


class Function(Module):
    def __init__(self, function, *, name=None, effect="local", idempotent=None, timeout=60):
        self.function, self.timeout = function, timeout
        self._signature = signature(function)
        hints = get_type_hints(function)
        self.input_model = create_model(function.__name__ + "Input", __config__=ConfigDict(extra="forbid"), **{
            name: (hints.get(name, Any), ... if p.default is p.empty else p.default)
            for name, p in self._signature.parameters.items()
        })
        self.output_adapter = TypeAdapter(hints.get("return", Any))
        self.spec = ToolSpec(name=name or "python." + function.__name__, description=inspect.getdoc(function) or "",
                             input_schema=self.input_model.model_json_schema(), output_schema=self.output_adapter.json_schema(),
                             effect=effect, idempotent=(effect != "write") if idempotent is None else idempotent)
        try:
            source = inspect.getsource(function).encode()
        except (OSError, TypeError):
            source = marshal.dumps(function.__code__)
        self.fingerprint = hashlib.sha256(source + self.spec.model_dump_json().encode()).hexdigest()

    @property
    def input_signature(self):
        return self._signature

    @property
    def input_schema(self):
        return self.spec.input_schema

    def forward(self, *args, **kwargs):
        bound = self._signature.bind(*args, **kwargs)
        bound.apply_defaults()
        trace = _trace.get()
        trace.register(self)
        return trace.node("tool", self.spec.name, bound.arguments, timeout_seconds=self.timeout)

    async def handler(self, arguments, context):
        import asyncio
        # Keep Pydantic instances for typed Python parameters, serialize only at the boundary.
        values = dict(self.input_model.model_validate(arguments))
        token = _executing_function.set(True)
        try:
            if inspect.iscoroutinefunction(self.function):
                output = await self.function(**values)
            else:
                output = await asyncio.to_thread(self.function, **values)
        finally:
            _executing_function.reset(token)
        return self.output_adapter.dump_python(self.output_adapter.validate_python(output), mode="json")


def tool(function=None, **options):
    """Turn an annotated sync/async Python function into a reusable tool/module."""
    return Function(function, **options) if function is not None else lambda fn: Function(fn, **options)


class Node(Function):
    """One scheduled operation with one input/output boundary.

    The function may combine ordinary Python operations, including async helpers.
    Retries and approvals apply to the whole node; use a workflow when individual
    operations need their own checkpoints. Node is also usable as an Agent tool.
    """


def node(function=None, **options):
    """Define a single reusable node from a sync/async Python function."""
    return Node(function, **options) if function is not None else lambda fn: Node(fn, **options)


class Agent(Module):
    def __init__(self, model=None, *, instructions=None, tools=(), **options):
        import os
        self.model = model or os.environ.get("EAH_MODEL")
        if not self.model:
            raise ValueError("Set EAH_MODEL or pass Agent(model='your-model')")
        self.tools = list(tools)
        names = [t.spec.name if isinstance(t, Function) else t for t in self.tools]
        self.config = AgentConfig(prompt="", tools=names, **({"instructions": instructions} if instructions else {}), **options)

    def forward(self, prompt: str):
        trace = _trace.get()
        for item in self.tools:
            if isinstance(item, Function):
                trace.register(item)
        trace.models.setdefault(self.model, set()).update({"chat", "decision"})
        arguments = {**self.config.model_dump(exclude_none=True), "prompt": prompt}
        output = trace.node("agent", self.model, arguments)
        return output["data" if self.config.response_schema else "text"]


class Model(Module):
    """One model call; returns the complete ModelResult (text, data, images, usage)."""
    def __init__(self, model, *, capability="chat", requires_approval=False, **parameters):
        self.model, self.capability = model, capability
        self.parameters, self.approval = parameters, requires_approval

    def forward(self, prompt: str):
        trace = _trace.get()
        trace.models.setdefault(self.model, set()).add(self.capability)
        return trace.node("model", self.model, {"prompt": prompt, "capability": self.capability,
                           "parameters": self.parameters}, requires_approval=self.approval)


class Call(Module):
    """Invoke an already-registered tool/API node; dict input and unmodified output."""
    def __init__(self, name, *, revision=None, requires_approval=False, timeout=60):
        self.name, self.revision, self.approval, self.timeout = name, revision, requires_approval, timeout

    def forward(self, inputs: dict):
        if not isinstance(inputs, (dict, Ref)):
            raise TypeError("Call expects explicit named arguments, e.g. Call('core.echo')({'text': ref})")
        return _trace.get().node("tool", self.name, inputs, tool_revision=self.revision,
                                 requires_approval=self.approval, timeout_seconds=self.timeout)


class Artifact(Module):
    """Save text or JSON as a run-owned artifact and return its metadata."""

    def __init__(self, name, *, media_type="application/json"):
        self.name, self.media_type = name, media_type

    def forward(self, content):
        return _trace.get().node("artifact", "", {
            "name": self.name, "content": content, "media_type": self.media_type})


class Subflow(Module):
    """Run a Module or a saved workflow as an independently recorded child run."""
    def __init__(self, identifier, *, revision=None):
        if not isinstance(identifier, (str, Module)):
            raise TypeError("Subflow requires a Module or saved workflow id")
        if isinstance(identifier, Module) and revision is not None:
            raise ValueError("revision only applies to a saved workflow id")
        self.identifier, self.revision = identifier, revision

    @property
    def input_signature(self):
        if isinstance(self.identifier, Module):
            return self.identifier.input_signature
        return inspect.Signature([inspect.Parameter("inputs", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=dict)])

    @property
    def input_schema(self):
        if isinstance(self.identifier, Module):
            return self.identifier.input_schema
        return {"type": "object", "properties": {"inputs": {"type": "object"}}, "required": ["inputs"], "additionalProperties": False}

    def forward(self, *args, **kwargs):
        trace = _trace.get()
        if isinstance(self.identifier, Module):
            bound = self.input_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            flow, child = self.identifier.build()
            for function in child.functions.values():
                trace.register(function)
            for name, capabilities in child.models.items():
                trace.models.setdefault(name, set()).update(capabilities)
            return trace.node("subworkflow", "", bound.arguments, body=flow.model_dump())["outputs"][0]["result"]
        inputs = self.input_signature.bind(*args, **kwargs).arguments["inputs"]
        if not isinstance(inputs, (dict, Ref)):
            raise TypeError("Subflow expects a dict of named business inputs")
        return _trace.get().node("subworkflow", "", inputs,
                                 workflow_ref={"id": self.identifier, "revision": self.revision})["outputs"][0]
