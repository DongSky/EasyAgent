"""Run offline functional demos against real SQLite, processes and HTTP.

Run: uv run python -m examples.demos.run_all
No API key needed. Provider results are explicitly mock fixtures.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn

from easyagent.api import create_app
from easyagent.client import HubClient
from easyagent.contracts import EvaluationSuite, Policy, ToolSpec
from easyagent.mcp_bridge import MCPConnection
from easyagent.plugins import load_plugin
from easyagent.runtime import Hub
from easyagent.studio import install_studio

ROOT = Path(__file__).resolve().parents[2]


async def command(*args, env=None):
    process = await asyncio.create_subprocess_exec(*args, cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async with asyncio.timeout(240):
        stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


async def main():
    report = []
    with tempfile.TemporaryDirectory(prefix="easyagent-demo-") as directory:
        hub = Hub(Path(directory) / "demo.db", poll_seconds=0.02)
        hub.skills.discover(ROOT / "examples/skills")
        hub.store.memory_put("family", "moving", {"owner": "Alex", "date": "2026-10-01"}, "demo fixture")
        await command("cargo", "build", "--quiet", "--manifest-path", "sdk/rust/Cargo.toml", "--examples")
        commands = {
            "python": [sys.executable, str(ROOT / "examples/plugins/python_plugin.py")],
            "javascript": ["node", str(ROOT / "examples/plugins/javascript_plugin.mjs")],
            "rust": [str(ROOT / "sdk/rust/target/debug/examples" / ("plugin.exe" if os.name == "nt" else "plugin"))],
        }
        for language, invocation in commands.items():
            path = Path(directory) / (language + ".json")
            path.write_text(json.dumps({"name": language, "command": invocation, "tools": [{"name": language + ".add"}]}))
            load_plugin(hub.tools, path)
        mcp = MCPConnection(command=sys.executable, args=[str(ROOT / "examples/demos/mcp_server.py")])
        await mcp.import_tools(hub.tools, "mcp", {"add": {"effect": "read", "idempotent": True}})

        async def demo_write(args, context):
            return {"receipt": context.invocation_id, "note": args["note"]}

        hub.tools.register(ToolSpec(name="demo.write", effect="write", idempotent=True), demo_write)
        app = create_app(hub)
        install_studio(app, hub)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            async with HubClient(url) as client:
                steps = [{"id": lang, "target": lang + ".add", "input": {"a": 20, "b": 22}} for lang in commands]
                steps += [
                    {"id": "mcp", "target": "mcp.add", "input": {"a": 1, "b": 2}},
                    {"id": "decision", "kind": "model", "target": "mock", "input": {"capability": "decision",
                     "prompt": '{"route":"travel"}', "response_schema": {"type": "object", "required": ["route"]}}},
                    {"id": "image", "kind": "model", "target": "mock", "input": {"capability": "image", "prompt": "fixture image"}},
                    {"id": "embedding", "kind": "model", "target": "mock", "input": {"capability": "embedding", "prompt": "fixture vector"}},
                    {"id": "agent", "kind": "agent", "target": "mock", "input": {"prompt": json.dumps({"tool": "memory.search",
                     "arguments": {"namespace": "family", "query": "moving"}}), "tools": ["memory.search"],"memory_namespaces":["family"], "skills": ["careful-planner"]}},
                    {"id": "branch", "target": "core.echo", "depends_on": ["decision"],
                     "when": {"source": "decision.data.route", "equals": "travel"}, "input": {"route": {"$ref": "decision.data.route"}}},
                ]
                run = await client.wait((await client.submit({"name": "all capabilities", "steps": steps}))["id"])
                assert run["status"] == "succeeded", run
                report.append({"demo": "DAG / 3 plugins / MCP / decision / image / embedding / Agent / Skills / memory", "status": run["status"],
                               "steps": len(run["steps"]), "model": "mock fixtures, not live model quality"})
                pending = await client.wait((await client.submit({"name": "approval", "steps": [
                    {"id": "write", "target": "demo.write", "input": {"note": "demo only"}}]}))["id"])
                assert pending["status"] == "waiting_approval"
                await client.approve(pending["approvals"][0]["id"])
                assert (await client.wait(pending["id"]))["status"] == "succeeded"
                report.append({"demo": "human approval + durable receipt", "status": "passed"})
                for instructions in ("be concise", "be concise and preserve dates"):
                    candidate = hub.evolution.propose(Policy(name="planner", model="mock", instructions=instructions))
                    await hub.evolution.evaluate(candidate["id"], EvaluationSuite(cases=[{"prompt": "confirmed", "expected": "confirmed"}]))
                    hub.evolution.activate(candidate["id"])
                    if instructions == "be concise":
                        original = candidate["id"]
                hub.evolution.activate(original, rollback=True)
                report.append({"demo": "self-evolve evaluation / activation / rollback", "status": "passed"})
                saved = await client.request("POST", "/v1/studio/assistants", {"name": "No-code demo", "purpose": "Organize tasks"})
                result = await client.request("POST", f"/v1/studio/assistants/{saved['id']}/run", {"message": "hello"})
                assert (await client.wait(result["id"]))["status"] == "succeeded"
                report.append({"demo": "no-code assistant -> shared runtime", "status": "passed"})
                env = {**os.environ, "EAH_URL": url}
                for invocation in (["node", "sdk/javascript/demo.js"],
                    ["cargo", "run", "--quiet", "--manifest-path", "sdk/rust/Cargo.toml", "--example", "demo"]):
                    report.append(json.loads(await command(*invocation, env=env)))
                report.append({"demo": "Python SDK + JS SDK + Rust SDK over real HTTP", "status": "passed"})
        finally:
            server.should_exit = True
            await serving
            sock.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
