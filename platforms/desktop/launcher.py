"""Standalone desktop entry; Python and pure code runtimes are bundled by PyInstaller."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import webbrowser


def default_data_directory(base):
    """Use the canonical application directory; explicit paths use EAH_DATA_DIR."""
    return Path(base) / "EasyAgent"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--eah-python":
        from easyagent.python_worker import main as python_worker

        python_worker(sys.argv[2])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--eah-worker":
        if sys.argv[2] == "wasm":
            from easyagent.wasm_worker import execute

            print(json.dumps(execute(json.loads(sys.stdin.buffer.readline(2_000_001)))))
        else:
            from easyagent.extension_worker import main as worker

            worker()
        return
    import uvicorn
    from easyagent.runtime import Hub
    from easyagent.api import create_app
    from easyagent.studio import install_studio
    from easyagent_app import mount_app

    if "--smoke" in sys.argv:
        import tempfile
        from easyagent.extensions import build_package

        async def smoke():
            with tempfile.TemporaryDirectory() as directory:
                hub = Hub(Path(directory) / "hub.db", poll_seconds=0.02)
                await hub.execution.initialize_local()
                await hub.start()
                try:
                    package = build_package(
                        {
                            "id": "smoke",
                            "revision": 1,
                            "title": "smoke",
                            "tools": [{"handler": "echo", "spec": {"name": "smoke.echo"}}],
                        },
                        {"extension.js": "function handle(r){return {result:r.params};}"},
                    )
                    await hub.extensions.install({"package": package})
                    r = await hub.wait(
                        hub.submit(
                            {
                                "name": "packaged",
                                "steps": [{"id": "x", "target": "smoke.echo", "input": {"bundled": True}}],
                            }
                        )
                    )
                    assert r["status"] == "succeeded", r
                    terminal = await hub.execution.terminal("execute", {"python":
                        "from PIL import Image; from pathlib import Path; "
                        "Image.new('RGB', (23, 17), 'blue').save('smoke.png'); print('Python 图片已保存')"
                    }, None)
                    assert terminal["exit_code"] == 0, terminal
                    assert "Python 图片已保存" in terminal["stdout"], terminal
                    failed = await hub.execution.terminal("execute", {"python": "raise ValueError('smoke-script-error')"}, None)
                    assert failed["exit_code"] != 0 and "smoke-script-error" in failed["stderr"], failed
                    media = await hub.wait(hub.submit({"name": "local image", "steps": [
                        {"id": "imported", "target": "attachments.import_file", "input": {"path": "smoke.png"}},
                        {"id": "verified", "target": "attachments.inspect_image", "depends_on": ["imported"],
                         "input": {"artifact_id": {"$ref": "imported.artifact.id"}}},
                    ]}))
                    assert media["status"] == "succeeded", media
                    assert media["steps"][-1]["output"]["image"]["width"] == 23, media
                    app = create_app(hub, manage_workers=False)
                    install_studio(app, hub)
                    mount_app(app)
                    assert len(app.openapi()["paths"]) > 50
                    print(
                        json.dumps(
                            {
                                "packaged_runtime": True,
                                "pure_extension": r["steps"][0]["output"],
                                "bundled_python": True,
                                "image_import_verified": True,
                                "openapi_paths": len(app.openapi()["paths"]),
                            }
                        )
                    )
                finally:
                    await hub.stop()

        asyncio.run(smoke())
        return
    base = (
        Path.home() / "Library/Application Support"
        if sys.platform == "darwin"
        else Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
    )
    root = Path(os.environ["EAH_DATA_DIR"]) if os.environ.get("EAH_DATA_DIR") else default_data_directory(base)
    root.mkdir(parents=True, exist_ok=True)

    async def serve():
        hub = Hub(root / "hub.db")
        await hub.execution.initialize_local()
        app = create_app(hub)
        install_studio(app, hub)
        mount_app(app)
        from examples.life_assistant.app import install_life

        install_life(app, hub)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        port = sock.getsockname()[1]
        (root / "runtime.json").write_text(
            json.dumps({"url": f"http://127.0.0.1:{port}", "pid": os.getpid()}), encoding="utf-8"
        )
        if not os.environ.get("EAH_DESKTOP_NO_BROWSER"):
            asyncio.get_running_loop().call_later(1, webbrowser.open, f"http://127.0.0.1:{port}")
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)).serve(
            sockets=[sock]
        )

    asyncio.run(serve())


if __name__ == "__main__":
    main()
