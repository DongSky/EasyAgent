"""Local/CLI/remote/app boundary acceptance using real processes and HTTP."""
import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap

import httpx
import pytest

from conftest import live_server
from easyagent import Agent, Call, Module, Runtime, tool
from easyagent.api import create_app
from easyagent.contracts import ModelResult
from easyagent_client import RunStopped
from easyagent_app import create_app as create_frontend

ROOT = Path(__file__).resolve().parents[2]


async def process(*args, cwd=None, stdin=None):
    env = {k: v for k, v in os.environ.items() if k in
           ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA")}
    child = await asyncio.create_subprocess_exec(sys.executable, *map(str, args), cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(child.communicate(stdin), 40)
        return child.returncode, out.decode(), err.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_local_modules_compile_branching_dag_tools_and_model_without_web(tmp_path):
    script = textwrap.dedent('''
        import importlib.abc, sys
        class NoWeb(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                if fullname.split('.')[0] in ('fastapi','starlette','uvicorn','easyagent_app','playwright','mcp'):
                    raise AssertionError('unexpected dependency: ' + fullname)
        sys.meta_path.insert(0, NoWeb())
        from easyagent import Agent, Module, Runtime, Sequential, tool
        @tool
        def clean(text: str) -> str: return text.strip()
        @tool
        def combine(left: str, right: str) -> dict: return {'left':left,'right':right}
        class Compare(Module):
            def forward(self, text: str):
                value = clean(text)
                return combine(Agent('mock')(value), Agent('mock')(value))
        flow = Compare()
        graph = flow.workflow()
        assert len(graph.steps) == 4
        assert graph.steps[-1].depends_on == ['step_2', 'step_3']
        with Runtime('local.db') as runtime:
            assert flow(' hello ') == {'left':'hello','right':'hello'}
            assert Sequential(clean, Agent('mock'))(' world ') == 'world'
            result = runtime.run(flow, text=' saved ')
            assert len(runtime.hub.store.events(result.id)) > 4
            assert result.value == {'left':'saved','right':'saved'}
        print('passed')
    ''')
    code, out, err = await process("-c", script, cwd=tmp_path)
    assert code == 0, err
    assert out.strip() == "passed"


async def test_async_sdk_tool_agent_extension_points_and_export(hub, tmp_path):
    calls = []

    @tool
    async def add(a: int, b: int = 2) -> int:
        calls.append((a, b))
        return a + b

    class Provider:
        async def generate(self, request, model):
            return ModelResult(text="done:" + request.messages[-1]["content"])

    hub.models.register("custom", Provider(), "custom", {"chat"})
    class Flow(Module):
        def forward(self, prompt: str):
            answer = Agent("custom")(prompt)
            return {"answer": answer, "sum": add(3), "echo": Call("core.echo")({"value": answer})}

    flow = Flow()
    graph = flow.workflow()
    assert calls == []  # Compilation is side-effect free.
    assert graph.steps[-1].depends_on == ["step_1"]
    flow.export(tmp_path / "flow.json")
    async with Runtime(hub=hub) as runtime:
        assert await flow.acall("hi") == {"answer": "done:hi", "sum": 5, "echo": {"value": "done:hi"}}
        assert calls == [(3, 2)]
        with pytest.raises(RuntimeError, match="event loop"):
            flow("hi")
        uploaded = runtime.upload(ROOT / "examples/first-workflow.json")
        assert runtime.hub.artifacts.get(uploaded["id"])[1]
    async with Runtime(tmp_path / "fresh.db") as runtime:
        with pytest.raises(ValueError, match="original @tool"):
            await runtime.arun(json.loads((tmp_path / "flow.json").read_text(encoding='utf-8')), prompt="hi")


async def test_cli_stdin_export_approval_restart_and_changed_code_rejection(tmp_path):
    source = tmp_path / "flow.py"
    source.write_text(textwrap.dedent('''
        from pathlib import Path
        from easyagent import tool
        @tool(effect='write')
        def flow(text: str) -> dict:
            print('diagnostic from tool')
            Path('receipt.txt').write_text(text)
            return {'written': text}
    '''), encoding='utf-8')
    base = ["-m", "easyagent.cli"]
    database = tmp_path / "state.db"
    code, out, err = await process(*base, "export", f"{source}:flow", cwd=tmp_path)
    assert code == 0, err
    assert json.loads(out)["steps"][0]["target"] == "python.flow"
    code, out, err = await process(*base, "run", f"{source}:flow", "--database", database,
                                   "--input", "-", "--key", "receipt", cwd=tmp_path, stdin=b'{"text":"saved"}')
    assert code == 3, err
    paused = json.loads(out)
    assert paused["status"] == "waiting_approval"
    assert not (tmp_path / "receipt.txt").exists()
    invocation = paused["approvals"][0]["id"]
    code, out, err = await process(*base, "approve", invocation, "--database", database, "--yes", cwd=tmp_path)
    assert code == 0, err
    original = source.read_text(encoding='utf-8')
    source.write_text(original.replace("return {'written': text}", "return {'different': text}"), encoding='utf-8')
    code, out, err = await process(*base, "resume", paused["id"], "--source", f"{source}:flow", "--database", database, cwd=tmp_path)
    assert code == 2 and "original @tool" in err
    assert not (tmp_path / "receipt.txt").exists()
    source.write_text(original, encoding='utf-8')
    code, out, err = await process(*base, "resume", paused["id"], "--source", f"{source}:flow", "--database", database, cwd=tmp_path)
    assert code == 0, err
    assert json.loads(out)["outputs"]["result"] == {"written": "saved"}
    assert "diagnostic from tool" in err
    assert (tmp_path / "receipt.txt").read_text(encoding='utf-8') == "saved"
    code, out, err = await process(*base, "run", f"{source}:flow", "--database", database,
                                 "--input", '{"text":"saved"}', "--key", "receipt", cwd=tmp_path)
    assert code == 0 and json.loads(out)["id"] == paused["id"], err
    assert "diagnostic from tool" not in err


async def test_cli_failed_and_timeout_exit_codes(tmp_path):
    source = tmp_path / "flow.json"
    source.write_text(json.dumps({"name": "waiting", "steps": [{"id": "a", "target": "core.echo", "not_before": 9999999999}]}), encoding='utf-8')
    code, out, err = await process("-m", "easyagent.cli", "run", source, "--database", tmp_path / "state.db", "--timeout", ".05", cwd=tmp_path)
    assert code == 4 and json.loads(out)["id"], err
    source.write_text(json.dumps({"name": "failure", "steps": [{"id": "a", "target": "core.fail", "max_attempts": 1}]}), encoding='utf-8')
    # Invalid tool binding is a configuration failure, not a silently successful command.
    code, out, err = await process("-m", "easyagent.cli", "run", source, "--database", tmp_path / "other.db", cwd=tmp_path)
    assert code == 2 and err


async def test_independent_app_api_auth_upload_and_stream(hub):
    service = create_app(hub, token="fixture-token", manage_workers=False)
    async with live_server(service) as backend:
        async with httpx.AsyncClient(base_url=backend) as direct:
            assert (await direct.get("/")).status_code == 404
            assert (await direct.get("/assets/studio.js")).status_code == 404
        async with live_server(create_frontend(backend)) as url, httpx.AsyncClient(base_url=url) as client:
            assert "EasyAgent" in (await client.get("/")).text
            assert (await client.get("/assets/studio.js")).status_code == 200
            assert (await client.get("/v1/models")).status_code == 401
            client.headers["Authorization"] = "Bearer fixture-token"
            assert (await client.get("/v1/models")).status_code == 200
            assert (await client.get("/v1/models", headers={"Origin": "https://elsewhere.test"})).status_code == 403
            client.headers["Origin"] = url
            upload = await client.post("/v1/artifacts/upload?name=test.txt", content=b"test", headers={"Content-Type": "text/plain"})
            assert upload.status_code == 201, upload.text
            assert (await client.get(f"/v1/artifacts/{upload.json()['id']}/content")).content == b"test"
            assert (await client.post("/v1/runs", content=b"a" * 2_000_001)).status_code == 413
            run = await client.post("/v1/runs", json={"name": "proxy", "steps": [{"id": "echo", "target": "core.echo", "input": {"ok": True}}]})
            assert run.status_code == 201, run.text
            stream = await client.get(f"/v1/runs/{run.json()['id']}/stream")
            assert "event: run.created" in stream.text and "succeeded" in stream.text
            assert (await client.get("/openapi.json")).json()["info"]["title"] == "EasyAgent"
            code, out, err = await process("-m", "easyagent.cli", "run", ROOT / "examples/first-workflow.json", "--url", url)
            assert code == 2 and "401" in err  # Token was not injected by the proxy.


async def test_python_pause_is_observable_without_ui(tmp_path):
    @tool(effect="write")
    async def send(text: str) -> str:
        return text
    async with Runtime(tmp_path / "approval.db") as runtime:
        with pytest.raises(RunStopped) as stopped:
            await runtime.arun(send, "hello")
        state = stopped.value.state
        runtime.hub.tools.approve(runtime.hub.store, state["approvals"][0]["id"], True)
        assert (await runtime.aresume(state["id"])).value == "hello"
