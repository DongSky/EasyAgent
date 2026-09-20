"""Headless commands. Importing this module never imports the HTTP service."""
from contextlib import redirect_stdout
import importlib.util
import json
from pathlib import Path
import sys

from .contracts import Workflow
from .local import Runtime
from .modules import Module
from .results import run_result


def load(source):
    filename, separator, attribute = source.rpartition(":")
    if not separator or not filename.endswith(".py"):
        filename, attribute = source, "flow"
    path = Path(filename).resolve()
    if path.suffix == ".py":
        # Explicit Python source is trusted executable code, like `python file.py`.
        spec = importlib.util.spec_from_file_location("_easyagent_entry", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        sys.path.insert(0, str(path.parent))
        try:
            with redirect_stdout(sys.stderr):
                spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        value = getattr(module, attribute)
        if not isinstance(value, (Module, Workflow, dict)):
            raise TypeError("Python entry must be a Module, Workflow or workflow dict")
        return value
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return Workflow.model_validate(yaml.safe_load(text))
    return Workflow.model_validate_json(text)


def read_inputs(value):
    if value == "-":
        text = sys.stdin.read()
    elif value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        text = value
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("input must be a JSON object")
    return result


async def remote(args, token):
    from .client import HubClient
    async with HubClient(args.url, token) as client:
        if args.command == "run":
            inputs = read_inputs(args.input)
            if Path(args.workflow).is_file() or ".py:" in args.workflow:
                value = load(args.workflow)
                flow = value.workflow() if isinstance(value, Module) else Workflow.model_validate(value)
                if flow.metadata.get("sdk_functions"):
                    raise ValueError("Python functions run locally; package and install them as extensions for remote use")
                flow.inputs.update(inputs)
                created = await client.submit(flow.model_dump(), idempotency_key=args.key)
                identifier = created["id"]
            else:
                identifier = (await client.workflow(args.workflow).start(inputs, key=args.key)).id
            return (await client.run_handle(identifier).result(timeout=args.timeout, poll_interval=.1)).state
        if args.command == "resume":
            return (await client.run_handle(args.run_id).result(timeout=args.timeout, poll_interval=.1)).state
        if args.command == "inspect":
            return await client.request("GET", f"/v1/runs/{args.run_id}/result")
        if args.command == "events":
            return await client.events(args.run_id)
        if args.command == "approve":
            return await client.approve(args.invocation_id, args.yes)


def command(args, token):
    if args.command == "export":
        value = load(args.source)
        flow = value.workflow() if isinstance(value, Module) else Workflow.model_validate(value)
        if args.output:
            Path(args.output).write_text(flow.model_dump_json(indent=2), encoding="utf-8")
            return {"output": args.output, "steps": len(flow.steps)}
        return flow.model_dump()
    if args.url:
        import asyncio
        return asyncio.run(remote(args, token))
    with Runtime(args.database, config=args.config, timeout=args.timeout, key=getattr(args, "key", None)) as runtime:
        if args.command == "run":
            if args.source:
                module = load(args.source)
                if isinstance(module, Module):
                    runtime.bind(module)
            value = load(args.workflow)
            with redirect_stdout(sys.stderr):
                return runtime.run(value, **read_inputs(args.input)).state
        if args.command == "resume":
            if args.source:
                module = load(args.source)
                if isinstance(module, Module):
                    runtime.bind(module)
            return runtime.resume(args.run_id).state
        if args.command == "inspect":
            return run_result(runtime.hub, args.run_id)
        if args.command == "events":
            return runtime.hub.store.events(args.run_id)
        if args.command == "approve":
            runtime.hub.tools.approve(runtime.hub.store, args.invocation_id, args.yes)
            return {"invocation_id": args.invocation_id, "approved": args.yes}
