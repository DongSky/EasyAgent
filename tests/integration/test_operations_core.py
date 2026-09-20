import asyncio
import json
from pathlib import Path
import pytest
import httpx
from easyagent.operations import backup, restore, diagnostics
from easyagent.runtime import Hub
from test_interop import subprocess_output


async def test_encrypted_backup_restore_and_diagnostics(hub, tmp_path):
    hub.connections.put_secret("backupkey", "never-in-plaintext")
    run = await hub.wait(
        hub.submit(
            {"name": "preserved", "steps": [{"id": "x", "target": "core.echo", "input": {"receipt": 42}}]}
        )
    )
    path = tmp_path / "backup.eah"
    backup(hub.store.path, path, "integration-password")
    assert b"never-in-plaintext" not in path.read_bytes()
    with pytest.raises(Exception):
        restore(path, tmp_path / "bad.db", "incorrect-password")
    restore(path, tmp_path / "restored.db", "integration-password")
    restored = Hub(tmp_path / "restored.db")
    assert restored.store.run(run["id"])["steps"][0]["output"] == {"receipt": 42}
    assert restored.connections.secret("backupkey") == "never-in-plaintext"
    assert diagnostics(restored)["healthy"]
    with pytest.raises(FileExistsError):
        restore(path, tmp_path / "restored.db", "integration-password")


async def test_backup_download_api_restores_secrets_and_trace(api, tmp_path):
    url, hub = api
    hub.connections.put_secret("downloadkey", "private-backup-fixture")
    run = await hub.wait(hub.submit({"name": "backup", "steps": [{"id": "x", "target": "core.echo"}]}))
    async with httpx.AsyncClient(base_url=url) as client:
        assert (await client.post("/v1/operations/backup", json={"password": "short"})).status_code == 422
        response = await client.post("/v1/operations/backup", json={"password": "download-fixture-password"})
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        link = (
            await client.post(
                "/v1/operations/backup?link=true", json={"password": "download-fixture-password"}
            )
        ).json()
        linked = await client.get(link["url"])
        assert linked.status_code == 200 and linked.content.startswith(b"EAHB1")
        assert linked.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"EAHB1")
        source = tmp_path / "download.eah"
        source.write_bytes(response.content)
        restore(source, tmp_path / "download-restored.db", "download-fixture-password")
        restored = Hub(tmp_path / "download-restored.db")
        assert restored.connections.secret("downloadkey") == "private-backup-fixture"
        assert restored.store.run(run["id"])["status"] == "succeeded"
        trace = (await client.get("/v1/operations/traces/" + run["id"])).json()
        assert trace["trace_id"] == run["id"] and trace["events"]
        assert "eah_workers" in (await client.get("/v1/operations/metrics")).text


async def test_shared_rust_core_host_approval_checkpoint_recovery(tmp_path):
    await subprocess_output(["cargo", "build", "--manifest-path", "core/Cargo.toml", "--example", "bridge"])
    import os

    exe = Path("core/target/debug/examples/bridge" + (".exe" if os.name == "nt" else "")).resolve()
    process = await asyncio.create_subprocess_exec(
        str(exe), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )

    async def send(body):
        process.stdin.write((json.dumps(body) + "\n").encode())
        await process.stdin.drain()
        result = json.loads(await process.stdout.readline())
        assert "error" not in result, result
        return result

    try:
        value = await send(
            {
                "op": "create",
                "run_id": "local1",
                "workflow": {
                    "steps": [
                        {"id": "sum", "kind": "transform", "input": {"value": {"text": "hello"}}},
                        {
                            "id": "send",
                            "target": "notify",
                            "depends_on": ["sum"],
                            "input": {"$ref": "sum.value"},
                        },
                    ]
                },
                "grants": {"notify": "write"},
            }
        )
        value = await send({"op": "next", "state": value["state"]})
        value = await send({"op": "next", "state": value["state"]})
        action = value["actions"][0]
        assert action["requires_approval"] and action["input"] == {"text": "hello"}
        value = await send(
            {
                "op": "apply",
                "state": value["state"],
                "event": {"type": "approve", "step_id": "send", "invocation_id": action["invocation_id"]},
            }
        )
        file = tmp_path / "checkpoint.json"
        file.write_text(json.dumps(value["state"]), encoding='utf-8')
        value = await send(
            {
                "op": "apply",
                "state": json.loads(file.read_text(encoding='utf-8')),
                "event": {"type": "recover", "step_id": "send", "invocation_id": action["invocation_id"]},
            }
        )
        assert value["state"]["nodes"]["send"]["status"] == "needs_attention"
        value = await send(
            {
                "op": "apply",
                "state": value["state"],
                "event": {
                    "type": "reconcile",
                    "step_id": "send",
                    "invocation_id": action["invocation_id"],
                    "output": {"delivered": True},
                    "receipt": "remote-id-1",
                },
            }
        )
        assert value["state"]["nodes"]["send"]["status"] == "succeeded"
    finally:
        process.stdin.close()
        await process.wait()
