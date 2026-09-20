import asyncio
import hashlib
import io
import json
import socket
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from easyagent import public_download
from easyagent.local_files import inspect_image
from easyagent.runtime import Hub

HTTP_CLIENT = httpx.AsyncClient


def png():
    out = io.BytesIO()
    Image.new("RGB", (37, 19), "red").save(out, format="PNG")
    return out.getvalue()


async def test_local_defaults_preserve_operator_choice_and_server_opt_in(hub):
    assert not hub.execution.settings().terminal_enabled
    assert "backend.terminal" not in {t["name"] for t in hub.available_tools()}
    await hub.execution.initialize_local()
    settings = hub.execution.settings()
    assert settings.terminal_enabled
    assert {"backend.terminal", "attachments.download", "attachments.inspect_image", "attachments.import_file"} <= {
        t["name"] for t in hub.available_tools()}
    await hub.execution.configure({**settings.model_dump(), "terminal_enabled": False})
    reopened = Hub(hub.store.path)
    await reopened.execution.initialize_local()
    assert not reopened.execution.settings().terminal_enabled
    assert reopened.execution.settings().workspace == settings.workspace


async def test_real_python_artifact_roundtrip_and_persistence(hub):
    await hub.execution.initialize_local()
    original = png()
    artifact = hub.artifacts.put("original.png", original, "image/png")
    run = await hub.wait(hub.submit({"name": "process real file", "steps": [
        {"id": "source", "target": "attachments.export_file", "input": {
            "artifact_id": artifact["id"], "path": "input/original.png"}},
        {"id": "script", "target": "backend.terminal", "depends_on": ["source"], "input": {
            "operation": "execute", "payload": {"python":
                "from PIL import Image\nwith Image.open('input/original.png') as im:\n"
                "    im.resize((74,38)).save('result.png')\nprint('已生成')"}}},
        {"id": "saved", "target": "attachments.import_file", "depends_on": ["script"],
         "input": {"path": "result.png", "require_image": True}},
        {"id": "verified", "target": "attachments.inspect_image", "depends_on": ["saved"],
         "input": {"artifact_id": {"$ref": "saved.artifact.id"}}},
    ]}))
    assert run["status"] == "waiting_approval", run
    hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
    run = await hub.wait(run["id"])
    assert run["status"] == "succeeded", run
    output = next(s for s in run["steps"] if s["id"] == "verified")["output"]
    assert output["image"]["width"] == 74 and output["image"]["height"] == 38
    assert output["image"]["decoded"]
    assert hub.artifacts.get(artifact["id"])[1] == original
    reopened = Hub(hub.store.path)
    assert inspect_image(reopened.artifacts.get(output["artifact"]["id"])[1])["width"] == 74
    shell = await hub.execution.terminal("execute", {"command": "echo shell-ok"}, None)
    assert shell["exit_code"] == 0 and "shell-ok" in shell["stdout"]


async def test_terminal_prelaunch_errors_are_not_uncertain_writes(hub):
    await hub.execution.initialize_local()
    for payload in ({"argv": ["easyagent-nonexistent-test-command"]},
                    {"argv": ["echo"], "python": "print(1)"}, {"command": "echo x", "cwd": ".."}):
        run = await hub.wait(hub.submit({"name": "invalid command", "steps": [{"id": "cmd",
            "target": "backend.terminal", "input": {"operation": "execute", "payload": payload}}]}))
        hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
        run = await hub.wait(run["id"])
        assert run["status"] == "failed", run
        assert any(e["kind"] == "tool.not_sent" for e in hub.store.events(run["id"]))


async def test_workspace_containment_and_no_overwrite(hub, tmp_path):
    await hub.execution.initialize_local()
    ctx = SimpleNamespace(run_id=None)
    artifact = hub.artifacts.put("file.txt", b"original")
    export = hub.tools.entry("attachments.export_file")[1]
    imported = hub.tools.entry("attachments.import_file")[1]
    await export({"artifact_id": artifact["id"], "path": "file.txt"}, ctx)
    await export({"artifact_id": artifact["id"], "path": "file.txt"}, ctx)
    other = hub.artifacts.put("file.txt", b"other")
    with pytest.raises(ValueError, match="已有不同内容"):
        await export({"artifact_id": other["id"], "path": "file.txt"}, ctx)
    for name in ("../hub.db", str(tmp_path / "hub.db")):
        with pytest.raises((ValueError, PermissionError)):
            await imported({"path": name}, ctx)
    from pathlib import Path
    link = Path(hub.execution.settings().workspace) / "escape"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return  # Windows environments without the symlink privilege.
    with pytest.raises(PermissionError):
        await imported({"path": "escape/hub.db"}, ctx)


def fake_network(monkeypatch, handler, addresses=None):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in (addresses(host) if addresses else ["93.184.216.34"])]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(public_download.httpx, "AsyncClient", lambda **kwargs:
                        HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


async def test_download_pins_dns_and_verifies_original_bytes(hub, monkeypatch):
    content = png()
    seen = []

    def request(req):
        seen.append(req)
        assert req.url.host == "93.184.216.34"
        assert req.extensions["sni_hostname"] == "images.example.org"
        assert req.headers["host"] == "images.example.org"
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/image", "set-cookie": "test=private"})
        assert "cookie" not in req.headers
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(content))

    fake_network(monkeypatch, request)
    result = await hub.tools.entry("attachments.download")[1](
        {"url": "https://images.example.org/start"}, SimpleNamespace(run_id=None))
    assert len(seen) == 2
    assert result["image"]["width"] == 37 and result["image"]["height"] == 19
    assert result["image"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert hub.artifacts.get(result["artifact"]["id"])[1] == content


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://localhost/test",
    str(httpx.URL("https://example.org/x").copy_with(username="fixture")),
    "http://127.0.0.1/x", "https://example.org:8765/x", "http://169.254.169.254/x", "http://[::1]/x"])
async def test_download_rejects_nonpublic_targets(monkeypatch, url):
    fake_network(monkeypatch, lambda r: pytest.fail("must not connect"), lambda h: [h])
    with pytest.raises(ValueError, match="公网"):
        await public_download.download(url)


async def test_download_rechecks_redirect_and_mixed_dns(monkeypatch):
    seen = []

    def request(req):
        seen.append(req)
        return httpx.Response(302, headers={"location": "https://private.example.org/image"})

    fake_network(monkeypatch, request, lambda h:
        ["93.184.216.34", "10.0.0.1"] if h == "private.example.org" else ["93.184.216.34"])
    with pytest.raises(ValueError, match="公网"):
        await public_download.download("https://images.example.org/x")
    assert len(seen) == 1


async def test_download_limits_and_invalid_images(hub, monkeypatch):
    fake_network(monkeypatch, lambda req: httpx.Response(200, stream=httpx.ByteStream(b"x" * 200)))
    with pytest.raises(ValueError, match="大小"):
        await public_download.download("https://example.org/file", max_bytes=100)
    fake_network(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "image/png"},
                                                       stream=httpx.ByteStream(b"<html>not an image</html>")))
    before = hub.artifacts.list()
    with pytest.raises(ValueError, match="可解码"):
        await hub.tools.entry("attachments.download")[1]({"url": "https://example.org/image"}, SimpleNamespace(run_id=None))
    assert hub.artifacts.list() == before
    with pytest.raises((ValueError, SyntaxError)):
        inspect_image(png()[:50])


async def test_execution_config_opt_out(hub, tmp_path):
    from easyagent.config import configure
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"execution": {"terminal_enabled": False}}), encoding="utf-8")
    await configure(hub, path)
    await hub.execution.initialize_local()
    assert not hub.execution.settings().terminal_enabled
