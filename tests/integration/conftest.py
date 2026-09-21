import asyncio
import os
import socket
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import expect

from easyagent.api import create_app
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from easyagent_app import mount_app


def pytest_configure():
    # Functional waits allow the same 30 seconds as Hub.wait and browser actions.
    # Hosted Windows storage can exceed the default five-second assertion window.
    expect.set_options(timeout=30_000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # Include setup/teardown, where an unresponsive subprocess can otherwise hang CI
    # for hours. asyncio timeouts cannot interrupt a blocked event-loop thread.
    timer = None
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        def expired():
            message = f'Test exceeded 120 seconds (including fixtures): {item.nodeid}'
            # os._exit bypasses pytest's captured-output and JUnit writers. Keep
            # the diagnosis outside capture so the next CI step can report it.
            report = Path('.eah/ci/test-timeout.txt')
            report.parent.mkdir(parents=True, exist_ok=True)
            with report.open('w', encoding='utf-8') as output:
                print(message, file=output)
                for identifier, frame in sys._current_frames().items():
                    print(f'Thread {identifier}:', file=output)
                    traceback.print_stack(frame, file=output)
            print('::error::' + message, file=sys.stderr, flush=True)
            for identifier, frame in sys._current_frames().items():
                print(f'Thread {identifier}:', file=sys.stderr)
                traceback.print_stack(frame, file=sys.stderr)
            sys.stderr.flush()
            os._exit(1)
        timer = threading.Timer(120, expired)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer:
            timer.cancel()


@asynccontextmanager
async def live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    # A Windows reset can leave asyncio.Server.wait_closed pending even after
    # Uvicorn has removed the connection. Bound cleanup so it cannot mask the
    # original request failure or prevent the application's lifespan shutdown.
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5))
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
        try:
            await asyncio.wait_for(task, 10)
        finally:
            sock.close()


@pytest.fixture
async def hub(tmp_path):
    # Normal integration tests must not accidentally become lease-expiry tests on busy CI hosts.
    # Crash/restart coverage sets its own short lease explicitly.
    instance = Hub(tmp_path / "hub.db", poll_seconds=0.01, lease_seconds=10)
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
