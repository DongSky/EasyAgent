import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx

from easyagent.mcp_bridge import MCPConnection
from easyagent.plugins import load_plugin

ROOT = Path(__file__).resolve().parents[2]


async def subprocess_output(command, env=None, timeout=120):
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE, cwd=ROOT, env=env)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout)
        assert process.returncode == 0, error.decode()
        return output.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_three_language_plugins_and_sdks(api, tmp_path):
    url, hub = api
    await subprocess_output(["cargo", "build", "--quiet", "--manifest-path", "sdk/rust/Cargo.toml", "--examples"], timeout=240)
    executable = "plugin.exe" if os.name == "nt" else "plugin"
    commands = {"python": [sys.executable, str(ROOT / "examples/plugins/python_plugin.py")],
                "javascript": ["node", str(ROOT / "examples/plugins/javascript_plugin.mjs")],
                "rust": [str(ROOT / "sdk/rust/target/debug/examples" / executable)]}
    steps = []
    for language, command in commands.items():
        manifest = {"api_version": "1", "name": language, "command": command, "tools": [{
            "name": language + ".add", "input_schema": {"type": "object", "required": ["a", "b"], "properties": {
                "a": {"type": "number"}, "b": {"type": "number"}}}}]}
        path = tmp_path / (language + ".json")
        path.write_text(json.dumps(manifest), encoding='utf-8')
        load_plugin(hub.tools, path)
        steps.append({"id": language, "target": language + ".add", "input": {"a": 2, "b": 3}})
    run = await hub.wait(hub.submit({"name": "three languages", "steps": steps}))
    assert run["status"] == "succeeded", run
    assert [s["output"]["value"] for s in run["steps"]] == [5, 5, 5]
    env = {**os.environ, "EAH_URL": url}
    js = json.loads(await subprocess_output(["node", "sdk/javascript/demo.js"], env))
    rs = json.loads(await subprocess_output(["cargo", "run", "--quiet", "--manifest-path", "sdk/rust/Cargo.toml", "--example", "demo"], env))
    assert js["output"]["language"] == "JavaScript"
    assert rs["output"]["language"] == "Rust"


async def test_mcp_stdio_http_and_hub_server(api):
    url, hub = api
    fixture = str(ROOT / "examples/demos/mcp_server.py")
    connection = MCPConnection(command=sys.executable, args=[fixture])
    await connection.import_tools(hub.tools, "stdio", {"add": {"effect": "read", "idempotent": True}})
    run = await hub.wait(hub.submit({"name": "MCP", "steps": [{"id": "add", "target": "stdio.add", "input": {"a": 7, "b": 8}}]}))
    assert run["status"] == "succeeded", run
    assert json.loads(run["steps"][0]["output"]["content"][0]["text"])["value"] == 15
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    process = await asyncio.create_subprocess_exec(sys.executable, fixture, "--http", "--port", str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with httpx.AsyncClient(timeout=30) as client, asyncio.timeout(30):
            while True:
                try:
                    await client.get(f"http://127.0.0.1:{port}/mcp")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        connection = MCPConnection(url=f"http://127.0.0.1:{port}/mcp")
        await connection.import_tools(hub.tools, "http", {"add": {"effect": "read", "idempotent": True}})
        run = await hub.wait(hub.submit({"name": "MCP HTTP", "steps": [{"id": "add", "target": "http.add", "input": {"a": 3, "b": 4}}]}))
        assert run["status"] == "succeeded", run
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)
    exported = MCPConnection(command=sys.executable, args=["-m", "easyagent.cli", "mcp", "--url", url])
    async with exported.session() as session:
        catalog = await session.list_tools()
        assert {"submit_workflow", "get_run", "list_tools"} <= {t.name for t in catalog.tools}
        submitted = await session.call_tool("submit_workflow", {"workflow": {"name": "via MCP", "steps": [{"id": "a", "target": "core.echo"}]}})
        result = submitted.structuredContent or json.loads(submitted.content[0].text)
        assert (await hub.wait(result["id"]))["status"] == "succeeded"


async def test_bad_plugin_and_output_limit_fail_safely(hub, tmp_path):
    script = tmp_path / "bad.py"
    script.write_text('import sys\nsys.stdin.readline()\nprint("x" * 5000)\n', encoding='utf-8')
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({"name": "bad", "command": [sys.executable, str(script)], "max_output_bytes": 1024,
                                    "tools": [{"name": "bad.output"}]}), encoding='utf-8')
    load_plugin(hub.tools, manifest)
    run = await hub.wait(hub.submit({"name": "bad plugin", "steps": [{"id": "a", "target": "bad.output"}]}))
    assert run["status"] == "failed"
    assert "output exceeds" in run["steps"][0]["error"]
