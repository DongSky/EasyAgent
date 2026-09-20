"""Local execution, enabled by local App launchers and opt-in for API servers."""

from __future__ import annotations

import asyncio
import os
import json
import time
import platform
import sys
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field

from .contracts import Contract
from .extension_process import terminate
from .tools import ToolPreparationError


class ExecutionSettings(Contract):
    terminal_enabled: bool = False
    browser_enabled: bool = False
    workspace: str = ""
    browser_origins: list[str] = Field(default_factory=list, max_length=50)
    headless: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class LocalExecution:
    def __init__(self, hub):
        self.hub = hub
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.playwright = None
        self.last_used = {}
        with hub.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS browser_profiles(root_id TEXT PRIMARY KEY, touched REAL NOT NULL)"
            )
        hub.backends.local.update(terminal=self.terminal, browser=self.browser)

    def settings(self):
        rows = self.hub.store.memory_search("execution-settings", limit=1)
        return ExecutionSettings.model_validate(rows[0]["value"] if rows else {})

    async def initialize_local(self):
        """First local App launch only; never override a saved operator choice."""
        if not self.hub.store.memory_search("execution-settings", limit=1):
            workspace = Path(self.hub.store.path).resolve().parent / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            await self.configure({"terminal_enabled": True, "workspace": str(workspace)})
        return self.settings()

    def environment(self):
        settings = self.settings()
        return {
            "system": platform.system(),
            "workspace": settings.workspace,
            "terminal_enabled": settings.terminal_enabled,
            "terminal_backend": "extension" if self.hub.backends.binding("terminal") else "builtin",
            "shell": "PowerShell" if os.name == "nt" else "/bin/sh",
            "python": "payload.python runs a Python script using the bundled interpreter; no system Python needed",
            "timeout_seconds": settings.timeout_seconds,
            "permissions": "Commands run as the current OS user under the task execution mode; workspace is not an OS sandbox.",
        }

    async def configure(self, body):
        s = ExecutionSettings.model_validate(body)
        if s.terminal_enabled:
            path = Path(s.workspace).expanduser().resolve()
            if not path.is_dir():
                raise ValueError("select an existing execution workspace")
            s.workspace = str(path)
        for origin in s.browser_origins:
            p = urlparse(origin)
            if (
                p.scheme not in ("http", "https")
                or p.username
                or p.password
                or p.path not in ("", "/")
                or not p.hostname
            ):
                raise ValueError("browser origins must be exact HTTP(S) origins")
        s.browser_origins = [o.rstrip("/") for o in s.browser_origins]
        if s.browser_enabled and not s.browser_origins:
            raise ValueError("select allowed browser sites first")
        await self.close()
        self.hub.store.memory_put("execution-settings", "local", s.model_dump(), "operator")
        return s.model_dump()

    async def terminal(self, operation, payload, job):
        s = self.settings()
        if not s.terminal_enabled:
            raise ToolPreparationError("enable built-in local command execution in settings first; no API key needed")
        if operation != "execute":
            raise ToolPreparationError("unsupported terminal operation")
        if sum(key in payload for key in ("argv", "command", "python")) != 1:
            raise ToolPreparationError("provide exactly one of argv, command, python")
        argv = payload.get("argv")
        for key in ("command", "python"):
            if key in payload and (not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 16000 or "\x00" in payload[key]):
                raise ToolPreparationError(key + " must be a nonempty string within 16000 characters")
        if "command" in payload:
            argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", payload["command"]]
                    if os.name == "nt" else ["/bin/sh", "-c", payload["command"]])
        if "python" in payload:
            argv = [sys.executable, "--eah-python" if getattr(sys, "frozen", False) else "-c", payload["python"]]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 100
            or any(not isinstance(a, str) or len(a) > 16000 or "\x00" in a for a in argv)
        ):
            raise ToolPreparationError("terminal payload needs argv: an array of executable and arguments")
        if not isinstance(payload.get("cwd", "."), str) or "\x00" in payload.get("cwd", "."):
            raise ToolPreparationError("cwd must be a workspace-relative directory")
        cwd = (Path(s.workspace) / payload.get("cwd", ".")).resolve()
        if not cwd.is_relative_to(Path(s.workspace)) or not cwd.is_dir():
            raise ToolPreparationError("working directory exceeds configured workspace")
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "TEMP", "TMP")
        }
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as exc:
            raise ToolPreparationError("command could not start; check executable and working directory") from exc

        async def read(stream):
            chunks = []
            size = 0
            while chunk := await stream.read(8192):
                size += len(chunk)
                if size > 400_000:
                    raise ValueError("command output exceeds limit")
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace")

        try:
            async with asyncio.timeout(s.timeout_seconds):
                stdout, stderr, _ = await asyncio.gather(
                    read(process.stdout), read(process.stderr), process.wait()
                )
            return {
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "workspace": str(cwd),
            }
        finally:
            await terminate(process)

    async def browser(self, operation, payload, job):
        s = self.settings()
        if not s.browser_enabled:
            raise PermissionError("enable browser execution and allowed sites in settings first")
        key = job["run_id"] if job else "operator"
        # Follow parent identity so goal revisions use the same browser session.
        with self.hub.store.connect() as db:
            while row := db.execute("SELECT parent_id FROM child_runs WHERE child_id=?", (key,)).fetchone():
                key = row[0]
        async with self.lock:
            if operation == "close":
                if old := self.sessions.pop(key, None):
                    await old[0].close()
                self.last_used.pop(key, None)
                with self.hub.store.connect() as db:
                    db.execute("DELETE FROM vault WHERE name=?", ("browser.profile." + key,))
                    db.execute("DELETE FROM browser_profiles WHERE root_id=?", (key,))
                return {"closed": True}
            if key not in self.sessions:
                if len(self.sessions) >= 4:
                    oldest = min(self.sessions, key=lambda k: self.last_used.get(k, 0))
                    await self.sessions.pop(oldest)[0].close()
                    self.last_used.pop(oldest, None)
                from playwright.async_api import async_playwright

                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                browser = await self.playwright.chromium.launch(headless=s.headless)
                saved = None
                try:
                    saved = json.loads(self.hub.connections.secret("browser.profile." + key))
                except ValueError:
                    pass
                try:
                    context = await browser.new_context(
                        accept_downloads=False,
                        service_workers="block",
                        storage_state=saved.get("storage") if saved else None,
                    )
                except BaseException:
                    await browser.close()
                    raise

                async def route(r):
                    p = urlparse(r.request.url)
                    origin = f"{p.scheme}://{p.netloc}"
                    if origin not in s.browser_origins:
                        await r.abort()
                    else:
                        await r.continue_()

                await context.route("**/*", route)
                page = await context.new_page()
                page.set_default_timeout(s.timeout_seconds * 1000)
                self.sessions[key] = (browser, page)
                if saved and operation != "navigate":
                    p = urlparse(saved.get("url", ""))
                    if f"{p.scheme}://{p.netloc}" in s.browser_origins:
                        await page.goto(saved["url"], wait_until="domcontentloaded")
            _, page = self.sessions[key]
            if operation == "navigate":
                p = urlparse(payload.get("url", ""))
                if f"{p.scheme}://{p.netloc}" not in s.browser_origins:
                    raise PermissionError("browser site not granted")
                await page.goto(payload["url"], wait_until="domcontentloaded")
            elif operation in ("click", "fill"):
                locator = page.locator(payload.get("selector", ""))
                if await locator.count() != 1:
                    raise ValueError("selector must identify exactly one element")
                if operation == "click":
                    await locator.click()
                else:
                    await locator.fill(str(payload.get("value", "")))
            elif operation != "snapshot":
                raise ValueError("unknown browser operation")
            profile = json.dumps({"url": page.url, "storage": await page.context.storage_state()})
            if len(profile.encode()) > 1_000_000:
                raise ValueError("browser storage state exceeds 1 MB")
            self.hub.connections.put_secret(
                "browser.profile." + key,
                profile,
            )
            self.last_used[key] = time.time()
            with self.hub.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO browser_profiles VALUES(?,?)", (key, time.time()))
            return {
                "url": page.url,
                "title": await page.title(),
                "text": (await page.locator("body").inner_text())[:50000],
            }

    async def close(self):
        for browser, _ in list(self.sessions.values()):
            await browser.close()
        self.sessions.clear()
        self.last_used.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def cleanup(self):
        async with self.lock:
            for key in list(self.sessions):
                if time.time() - self.last_used.get(key, 0) > 600:
                    await self.sessions.pop(key)[0].close()
                    self.last_used.pop(key, None)
            with self.hub.store.connect() as db:
                db.execute(
                    "DELETE FROM vault WHERE name IN (SELECT 'browser.profile.' || root_id FROM browser_profiles WHERE touched<?)",
                    (time.time() - 7 * 86400,),
                )
                db.execute("DELETE FROM browser_profiles WHERE touched<?", (time.time() - 7 * 86400,))


def install_execution_api(app, hub):
    @app.get("/v1/execution/settings")
    async def get():
        return hub.execution.settings().model_dump()

    @app.put("/v1/execution/settings")
    async def put(body: ExecutionSettings):
        return await hub.execution.configure(body)
