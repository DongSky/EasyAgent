import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap

import httpx
import pytest

from easyagent.runtime import Hub
from platforms.desktop.launcher import default_data_directory


@pytest.mark.parametrize("first", ["easyagent", "independent"])
async def test_runtime_does_not_claim_another_projects_namespace(first, tmp_path):
    # This name is deliberately owned by an unrelated fixture, never an alias.
    independent = tmp_path / "easyagenthub"
    independent.mkdir()
    (independent / "__init__.py").write_text('OWNER = "independent-project"\n', encoding='utf-8')
    (independent / "contracts.py").write_text('class Workflow: pass\n', encoding='utf-8')
    script = textwrap.dedent('''
        import asyncio
        import importlib
        import importlib.util
        from importlib.resources import files
        from pathlib import Path
        import sys

        first, fixture, database = sys.argv[1:]
        assert importlib.util.find_spec("easyagenthub") is None
        sys.path.insert(0, fixture)
        importlib.import_module("easyagent.runtime" if first == "easyagent" else "easyagenthub.contracts")
        import easyagent
        import easyagent.runtime
        import easyagenthub
        import easyagenthub.contracts

        assert easyagenthub.OWNER == "independent-project"
        assert Path(easyagenthub.__file__).parent == Path(fixture) / "easyagenthub"
        assert easyagenthub.contracts.Workflow is not easyagent.Workflow
        assert easyagent.Workflow.__module__ == "easyagent.contracts"
        assert not files("easyagent").joinpath("static/studio.html").is_file()
        assert files("easyagent_app").joinpath("static/studio.html").is_file()
        assert files("easyagent").joinpath("data/model_protocols.json").is_file()

        async def main():
            hub = easyagent.runtime.Hub(database)
            await hub.start()
            try:
                result = await hub.wait(hub.submit({
                    "name": "namespace isolation",
                    "steps": [{"id": "echo", "target": "core.echo", "input": {"isolated": True}}],
                }))
                assert result["status"] == "succeeded"
                assert result["steps"][0]["output"] == {"isolated": True}
            finally:
                await hub.stop()
        asyncio.run(main())
        print("independent namespace and canonical runtime verified")
    ''')
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", script, first, str(tmp_path), str(tmp_path / "isolation.db"),
        cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, error.decode()
    assert b"independent namespace and canonical runtime verified" in output


async def test_canonical_commands_run_workflows_and_keep_http_contracts(api):
    url, hub = api
    root = Path(__file__).resolve().parents[2]
    flow = json.loads((root / "examples/first-workflow.json").read_text(encoding='utf-8'))
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        assert (await client.get("/openapi.json")).json()["info"]["title"] == "EasyAgent"
        assert "EasyAgent · 工作室" in (await client.get("/")).text
        created = await client.post("/v1/runs", json=flow, headers={"Idempotency-Key": "rename"})
        assert (await hub.wait(created.json()["id"]))["status"] == "succeeded"
        conflict = await client.post(
            "/v1/runs", json={**flow, "name": "different"}, headers={"Idempotency-Key": "rename"}
        )
        assert conflict.status_code == 409
    for command in (
        [sys.executable, "-m", "easyagent.cli"],
        [str(Path(sys.executable).parent / ("easyagent.exe" if os.name == "nt" else "easyagent"))],
    ):
        process = await asyncio.create_subprocess_exec(
            *command, "run", str(root / "examples/first-workflow.json"), "--url", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "EAH_TOKEN": ""},
        )
        output, error = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, error.decode()
        assert json.loads(output)["status"] == "succeeded"


async def test_desktop_reopens_projects_and_credentials_in_canonical_directory(tmp_path):
    root = default_data_directory(tmp_path)
    assert root == tmp_path / "EasyAgent"
    previous = Hub(root / "hub.db")
    previous.connections.put_secret("rename_proof", "synthetic-preserved-credential")
    run_id = previous.submit({"name": "Persisted project", "steps": [{"id": "echo", "target": "core.echo"}]})
    reopened = Hub(default_data_directory(tmp_path) / "hub.db")
    assert reopened.connections.secret("rename_proof") == "synthetic-preserved-credential"
    await reopened.start()
    try:
        assert (await reopened.wait(run_id))["status"] == "succeeded"
    finally:
        await reopened.stop()
