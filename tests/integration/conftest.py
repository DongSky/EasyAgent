import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn

from easyagent.api import create_app
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from easyagent_app import mount_app


@asynccontextmanager
async def live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.fixture
async def hub(tmp_path):
    instance = Hub(tmp_path / "hub.db", poll_seconds=0.01, lease_seconds=0.3)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def api(hub):
    app = create_app(hub, manage_workers=False)
    install_studio(app, hub)
    mount_app(app)
    async with live_server(app) as url:
        yield url, hub
